"""
IDM-style parallel chunked file downloader for Pyrogram.

Each worker fetches a 512 KB slice at a unique byte offset simultaneously
(like IDM's multi-segment download), writes directly to the correct position
in the pre-allocated output file, then signals progress.

Falls back: raises RuntimeError on DC-migration errors so the caller can
gracefully use the standard sequential download_media().
"""

import asyncio
import os
import time
import threading
import logging

from pyrogram import Client
from pyrogram.raw import functions as rf, types as rt

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
# 512 KB per chunk — must be a multiple of Telegram's 4 KB block size.
# Telegram's max for non-CDN files is 1 MB, but 512 KB gives better
# granularity for progress and keeps individual requests fast.
CHUNK_SIZE = 1 << 19      # 512 KB
MAX_WORKERS = 4            # parallel download streams

MIME_TO_EXT = {
    "video/mp4": ".mp4",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/octet-stream": ".bin",
    "text/plain": ".txt",
}


def ext_from_mime(mime: str) -> str:
    """Return a file extension for the given MIME type, e.g. '.mp4'."""
    return MIME_TO_EXT.get((mime or "").lower().split(";")[0].strip(), ".bin")


async def _get_raw_location(ub: Client, src_chat, msg_id: int):
    """
    Fetch the raw Telegram file location for a message via MTProto.

    Returns (InputFileLocation, file_size_bytes, mime_type_str).
    Supports documents (videos, files) and photos.
    Raises ValueError/RuntimeError on unsupported or missing media.
    """
    peer = await ub.resolve_peer(src_chat)

    if isinstance(peer, rt.InputPeerChannel):
        res = await ub.invoke(
            rf.channels.GetMessages(
                channel=rt.InputChannel(
                    channel_id=peer.channel_id,
                    access_hash=peer.access_hash,
                ),
                id=[rt.InputMessageID(id=msg_id)],
            )
        )
    else:
        res = await ub.invoke(
            rf.messages.GetMessages(id=[rt.InputMessageID(id=msg_id)])
        )

    if not res.messages or isinstance(res.messages[0], rt.MessageEmpty):
        raise RuntimeError("Message not found via raw API")

    raw_msg = res.messages[0]
    media = getattr(raw_msg, "media", None)

    if isinstance(media, rt.MessageMediaDocument):
        doc = media.document
        if not isinstance(doc, rt.Document):
            raise ValueError("Empty document object")
        return (
            rt.InputDocumentFileLocation(
                id=doc.id,
                access_hash=doc.access_hash,
                file_reference=doc.file_reference,
                thumb_size="",
            ),
            doc.size,
            doc.mime_type or "application/octet-stream",
        )

    if isinstance(media, rt.MessageMediaPhoto):
        photo = media.photo
        if not isinstance(photo, rt.Photo):
            raise ValueError("Empty photo object")
        photo_sizes = [s for s in photo.sizes if isinstance(s, rt.PhotoSize)]
        if not photo_sizes:
            raise ValueError("No photo sizes available")
        biggest = max(photo_sizes, key=lambda s: s.size)
        return (
            rt.InputPhotoFileLocation(
                id=photo.id,
                access_hash=photo.access_hash,
                file_reference=photo.file_reference,
                thumb_size=biggest.type,
            ),
            biggest.size,
            "image/jpeg",
        )

    raise ValueError(f"Unsupported media type for parallel download: {type(media).__name__}")


async def parallel_download(
    ub: Client,
    src_chat,
    msg_id: int,
    out_path: str,
    progress_cb=None,
) -> str:
    """
    Download a Telegram media file using parallel chunk workers (IDM-style).

    Args:
        ub:           Authenticated Pyrogram Client (userbot).
        src_chat:     Channel/chat ID or username containing the file.
        msg_id:       Message ID of the media message.
        out_path:     Absolute or relative path where the file will be saved.
        progress_cb:  Optional async callable(current_bytes, total_bytes, speed_bps).
                      Called after each chunk completes (throttled by caller).

    Returns:
        out_path (str) on success.

    Raises:
        RuntimeError if any chunks fail (DC migration, repeated errors).
        The caller should catch this and fall back to download_media().
    """
    file_loc, total_size, _mime = await _get_raw_location(ub, src_chat, msg_id)

    # Pre-allocate the full output file so random-offset writes work correctly
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.truncate(total_size)

    # ── Shared mutable state ──────────────────────────────────────────────────
    downloaded = [0]          # bytes confirmed written so far
    failed: list[int] = []   # offsets of permanently-failed chunks
    t0 = time.monotonic()

    # One shared file handle; writes are serialised by a threading.Lock
    # so seek+write pairs are always atomic even with multiple executor threads.
    _fh = open(out_path, "r+b")
    _wlock = threading.Lock()
    _sem = asyncio.Semaphore(MAX_WORKERS)
    _loop = asyncio.get_event_loop()

    # ── Per-chunk worker ──────────────────────────────────────────────────────
    async def fetch_chunk(offset: int):
        limit = min(CHUNK_SIZE, total_size - offset)
        data: bytes | None = None

        async with _sem:          # at most MAX_WORKERS running concurrently
            for attempt in range(4):
                try:
                    result = await ub.invoke(
                        rf.upload.GetFile(
                            location=file_loc,
                            offset=offset,
                            limit=limit,
                            cdn_supported=False,
                        )
                    )
                    data = result.bytes
                    break
                except Exception as exc:
                    err = str(exc)
                    # File lives on a different DC — raw approach won't work;
                    # signal the caller to use sequential download_media().
                    if any(k in err for k in ("FILE_MIGRATE", "STORAGE_MIGRATE",
                                              "FILE_TOKEN_INVALID")):
                        failed.append(offset)
                        return
                    wait = 2 ** attempt
                    logger.debug(
                        f"chunk@{offset} attempt {attempt+1} failed: {exc} "
                        f"— retry in {wait}s"
                    )
                    await asyncio.sleep(wait)
            else:
                # All 4 attempts exhausted
                failed.append(offset)
                return

        if not data:
            failed.append(offset)
            return

        # Write chunk at the correct file offset (non-blocking via threadpool)
        def _write():
            with _wlock:
                _fh.seek(offset)
                _fh.write(data)

        await _loop.run_in_executor(None, _write)

        downloaded[0] += len(data)
        if progress_cb:
            elapsed = time.monotonic() - t0 or 0.001
            try:
                await progress_cb(downloaded[0], total_size, downloaded[0] / elapsed)
            except Exception:
                pass

    # ── Launch all chunk workers ──────────────────────────────────────────────
    try:
        offsets = list(range(0, total_size, CHUNK_SIZE))
        await asyncio.gather(*[fetch_chunk(off) for off in offsets])
    finally:
        _fh.close()

    if failed:
        raise RuntimeError(
            f"{len(failed)} chunk(s) could not be downloaded "
            "(DC migration or repeated errors) — use sequential fallback"
        )

    return out_path

