import asyncio
import os
import re
import time
import logging
from io import BytesIO

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import main as _main_module
from .. import Bot, SUDO_USERS, DB_CHANNEL, SESSION, PROTECT_CONTENT, CUSTOM_CAPTION, DISABLE_CHANNEL_BUTTON
from main.plugins.link_helpers import encode, decode, get_db_messages, get_db_message_id

logger = logging.getLogger(__name__)

ADMINS = list(SUDO_USERS)

_bot_username_cache = None

# Files smaller than this are kept in RAM; larger ones go to a temp file.
_MEM_LIMIT = 80 * 1024 * 1024   # 80 MB


def _userbot():
    return _main_module.userbot


async def _get_bot_username(client: Client) -> str:
    global _bot_username_cache
    if not _bot_username_cache:
        me = await client.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


def _userbot_error() -> str:
    if not SESSION:
        return (
            "❌ <b>SESSION not set.</b>\n\n"
            "Add your Pyrogram string session as the <code>SESSION</code> env var and redeploy."
        )
    return (
        "❌ <b>Userbot failed to start.</b>\n\n"
        "SESSION is set but the session is invalid or expired.\n"
        "Generate a fresh Pyrogram session string and update the <code>SESSION</code> env var."
    )


def _parse_tme_link(link: str):
    link = link.strip()
    m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', link)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))
    m = re.match(r'https?://t\.me/([^/+][^/]*)/(\d+)', link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def _make_link_markup(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Share Link", url=f"https://telegram.me/share/url?url={link}")
    ]])


def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


async def _copy_msg_to_db(ub: Client, db_channel: int, src_chat, msg_id: int,
                          status_msg=None):
    """
    Copy one message to the DB channel.

    Attempt order:
      1. copy_message  — server-side, zero bandwidth (fails for noforwards channels)
      2. forward_messages(drop_author=True)  — server-side (also blocked for some channels)
      3. Download to temp file / RAM → re-upload with live progress shown in status_msg
    """

    # ── throttled status editor ────────────────────────────────────────────────
    _last_edit: list[float] = [0.0]

    async def _upd(text: str, force: bool = False):
        if status_msg is None:
            return
        now = time.time()
        if not force and now - _last_edit[0] < 3:
            return
        _last_edit[0] = now
        try:
            await status_msg.edit(text)
        except Exception:
            pass

    # ── Attempt 1: server-side copy ───────────────────────────────────────────
    try:
        return await ub.copy_message(
            chat_id=db_channel,
            from_chat_id=src_chat,
            message_id=msg_id,
        )
    except Exception as e:
        err = str(e)
        if "CHAT_FORWARDS_RESTRICTED" not in err:
            raise
        logger.info("copy_message blocked (noforwards). Trying forward_messages…")

    # ── Attempt 2: server-side forward with hidden author ────────────────────
    try:
        result = await ub.forward_messages(
            chat_id=db_channel,
            from_chat_id=src_chat,
            message_ids=msg_id,
            drop_author=True,
        )
        return result[0] if isinstance(result, list) else result
    except Exception as e:
        err = str(e)
        if "CHAT_FORWARDS_RESTRICTED" not in err and "CHAT_ADMIN_REQUIRED" not in err:
            raise
        logger.info("forward_messages also blocked. Falling back to download+upload…")

    # ── Attempt 3: download → re-upload (restricted channels) ─────────────────
    msg = await ub.get_messages(src_chat, msg_id)
    if not msg:
        raise RuntimeError("Message not found in source channel")

    caption = ""
    if msg.caption:
        caption = msg.caption.html
    elif msg.text:
        caption = msg.text

    if not msg.media:
        return await ub.send_message(db_channel, caption or "(empty message)")

    # Detect media + file size
    media = (msg.document or msg.video or msg.audio
             or msg.voice or msg.video_note or msg.photo)
    file_size: int = getattr(media, "file_size", 0) or 0
    size_label = _fmt_size(file_size) if file_size else "unknown size"

    # ── progress callbacks ────────────────────────────────────────────────────
    _t0 = [time.time()]

    async def dl_progress(current: int, total: int):
        elapsed = time.time() - _t0[0]
        speed = current / elapsed if elapsed > 0 else 0
        pct = current * 100 / total if total else 0
        await _upd(
            f"⬇️ <b>Downloading</b> ({size_label})\n"
            f"{pct:.0f}%  •  {_fmt_size(current)} / {_fmt_size(total)}\n"
            f"Speed: {_fmt_size(speed)}/s"
        )

    async def ul_progress(current: int, total: int):
        elapsed = time.time() - _t0[0]
        speed = current / elapsed if elapsed > 0 else 0
        pct = current * 100 / total if total else 0
        await _upd(
            f"⬆️ <b>Uploading</b> ({size_label})\n"
            f"{pct:.0f}%  •  {_fmt_size(current)} / {_fmt_size(total)}\n"
            f"Speed: {_fmt_size(speed)}/s"
        )

    # Choose storage: temp file for large, BytesIO for small
    use_temp = file_size > _MEM_LIMIT

    if use_temp:
        tmp_path = f"/tmp/fwd_{msg_id}_{int(time.time())}"
        await _upd(f"⬇️ Downloading ({size_label}) — 0%")
        _t0[0] = time.time()
        try:
            dl_path = await ub.download_media(
                msg, file_name=tmp_path, progress=dl_progress
            )
            await _upd(f"⬆️ Uploading ({size_label}) — 0%", force=True)
            _t0[0] = time.time()
            sent = await _send_media(ub, db_channel, msg, dl_path, caption,
                                     ul_progress)
        finally:
            try:
                os.remove(dl_path)
            except Exception:
                pass
        return sent
    else:
        await _upd(f"⬇️ Downloading ({size_label}) — 0%")
        _t0[0] = time.time()
        bio = await ub.download_media(msg, in_memory=True, progress=dl_progress)
        bio.seek(0)
        await _upd(f"⬆️ Uploading ({size_label}) — 0%", force=True)
        _t0[0] = time.time()
        return await _send_media(ub, db_channel, msg, bio, caption, ul_progress)


async def _send_media(ub: Client, db_channel: int, msg: Message,
                      file, caption: str, progress_cb=None):
    """Send the right media type to db_channel."""
    kw = dict(caption=caption, progress=progress_cb)
    if msg.video:
        return await ub.send_video(
            db_channel, file,
            duration=msg.video.duration,
            width=msg.video.width,
            height=msg.video.height,
            **kw,
        )
    elif msg.document:
        fname = getattr(msg.document, "file_name", None) or "document"
        return await ub.send_document(db_channel, file, file_name=fname, **kw)
    elif msg.audio:
        return await ub.send_audio(db_channel, file, **kw)
    elif msg.photo:
        return await ub.send_photo(db_channel, file, caption=caption,
                                   progress=progress_cb)
    elif msg.voice:
        return await ub.send_voice(db_channel, file, **kw)
    elif msg.video_note:
        return await ub.send_video_note(db_channel, file,
                                        progress=progress_cb)
    else:
        return await ub.send_document(db_channel, file, **kw)


# ── /fwd ──────────────────────────────────────────────────────────────────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("fwd"))
async def fwd_command(client: Client, message: Message):
    ub = _userbot()
    if not ub:
        return await message.reply(_userbot_error())
    if not DB_CHANNEL:
        return await message.reply(
            "❌ <b>DB_CHANNEL not set.</b>\n\n"
            "Set the <code>DB_CHANNEL</code> env var and redeploy."
        )

    uid = message.from_user.id
    _main_module.fwd_active_users.add(uid)
    try:
        try:
            asked = await client.ask(
                chat_id=uid,
                text=(
                    "📎 Send the restricted channel post link:\n"
                    "<code>https://t.me/c/CHATID/MSGID</code>"
                ),
                filters=filters.text & filters.private,
                timeout=60,
            )
        except Exception:
            return

        src_chat, msg_id = _parse_tme_link(asked.text.strip())
        if not src_chat or not msg_id:
            return await asked.reply(
                "❌ Invalid link format.\n"
                "Expected: <code>https://t.me/c/CHATID/MSGID</code>"
            )

        status = await asked.reply("⏳ Copying to DB channel…")
        try:
            sent = await _copy_msg_to_db(ub, DB_CHANNEL, src_chat, msg_id,
                                         status_msg=status)
        except Exception as e:
            return await status.edit(f"❌ Failed:\n<code>{e}</code>")

        username = await _get_bot_username(client)
        base64_string = await encode(f"get-{sent.id * abs(DB_CHANNEL)}")
        link = f"https://t.me/{username}?start={base64_string}"
        await status.edit(
            f"✅ <b>Done! Share link:</b>\n\n{link}",
            reply_markup=_make_link_markup(link),
        )
    finally:
        _main_module.fwd_active_users.discard(uid)


# ── /batchfwd ─────────────────────────────────────────────────────────────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("batchfwd"))
async def batchfwd_command(client: Client, message: Message):
    ub = _userbot()
    if not ub:
        return await message.reply(_userbot_error())
    if not DB_CHANNEL:
        return await message.reply(
            "❌ <b>DB_CHANNEL not set.</b>\n\n"
            "Set the <code>DB_CHANNEL</code> env var and redeploy."
        )

    uid = message.from_user.id
    _main_module.fwd_active_users.add(uid)
    try:
        while True:
            try:
                first_msg = await client.ask(
                    chat_id=uid,
                    text="📎 Send the link of the <b>FIRST</b> message:",
                    filters=filters.text & filters.private,
                    timeout=60,
                )
            except Exception:
                return
            src_chat, f_id = _parse_tme_link(first_msg.text.strip())
            if src_chat and f_id:
                break
            await first_msg.reply("❌ Invalid link. Try again.")

        while True:
            try:
                last_msg = await client.ask(
                    chat_id=uid,
                    text="📎 Send the link of the <b>LAST</b> message:",
                    filters=filters.text & filters.private,
                    timeout=60,
                )
            except Exception:
                return
            last_chat, l_id = _parse_tme_link(last_msg.text.strip())
            if last_chat and l_id:
                break
            await last_msg.reply("❌ Invalid link. Try again.")

        if str(src_chat) != str(last_chat):
            return await last_msg.reply("❌ Both links must be from the same channel.")
        if f_id > l_id:
            f_id, l_id = l_id, f_id

        ids = list(range(f_id, l_id + 1))
        total = len(ids)
        status = await last_msg.reply(f"⏳ Starting — {total} message(s) to copy…")

        db_start_id = db_end_id = None
        done = failed = 0

        for idx, msg_id in enumerate(ids, 1):
            try:
                await status.edit(f"⏳ [{idx}/{total}] Copying…")
            except Exception:
                pass
            try:
                sent = await _copy_msg_to_db(ub, DB_CHANNEL, src_chat, msg_id,
                                             status_msg=status)
                if db_start_id is None:
                    db_start_id = sent.id
                db_end_id = sent.id
                done += 1
            except FloodWait as e:
                await asyncio.sleep(e.value + 2)
                try:
                    sent = await _copy_msg_to_db(ub, DB_CHANNEL, src_chat, msg_id)
                    if db_start_id is None:
                        db_start_id = sent.id
                    db_end_id = sent.id
                    done += 1
                except Exception as e2:
                    logger.warning(f"Skipped msg {msg_id}: {e2}")
                    failed += 1
            except Exception as e:
                logger.warning(f"Skipped msg {msg_id}: {e}")
                failed += 1
            await asyncio.sleep(0.5)

        if db_start_id is None:
            return await status.edit("❌ No messages were copied.")

        username = await _get_bot_username(client)
        base64_string = await encode(
            f"get-{db_start_id * abs(DB_CHANNEL)}-{db_end_id * abs(DB_CHANNEL)}"
        )
        link = f"https://t.me/{username}?start={base64_string}"
        await status.edit(
            f"✅ Done! {done}/{total} copied (skipped {failed}).\n\n"
            f"<b>Share link:</b>\n{link}",
            reply_markup=_make_link_markup(link),
        )
    finally:
        _main_module.fwd_active_users.discard(uid)


# ── /genlink ──────────────────────────────────────────────────────────────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("genlink"))
async def genlink_command(client: Client, message: Message):
    if not DB_CHANNEL:
        return await message.reply("❌ DB_CHANNEL not configured.")

    uid = message.from_user.id
    _main_module.fwd_active_users.add(uid)
    try:
        while True:
            try:
                channel_message = await client.ask(
                    chat_id=uid,
                    text=(
                        "Forward a message from your DB Channel (with Quotes)\n"
                        "or send the DB Channel post link:"
                    ),
                    filters=(filters.forwarded | (filters.text & ~filters.forwarded)),
                    timeout=60,
                )
            except Exception:
                return
            msg_id = await get_db_message_id(client, channel_message, DB_CHANNEL)
            if msg_id:
                break
            await channel_message.reply(
                "❌ Not from my DB Channel. Try again."
            )

        username = await _get_bot_username(client)
        base64_string = await encode(f"get-{msg_id * abs(DB_CHANNEL)}")
        link = f"https://t.me/{username}?start={base64_string}"
        await channel_message.reply_text(
            f"<b>Here is your link</b>\n\n{link}",
            quote=True,
            reply_markup=_make_link_markup(link),
        )
    finally:
        _main_module.fwd_active_users.discard(uid)


# ── /mkbatch ──────────────────────────────────────────────────────────────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("mkbatch"))
async def mkbatch_command(client: Client, message: Message):
    if not DB_CHANNEL:
        return await message.reply("❌ DB_CHANNEL not configured.")

    uid = message.from_user.id
    _main_module.fwd_active_users.add(uid)
    try:
        while True:
            try:
                first_message = await client.ask(
                    chat_id=uid,
                    text=(
                        "Forward the <b>First</b> message from DB Channel (with Quotes)\n"
                        "or send its post link:"
                    ),
                    filters=(filters.forwarded | (filters.text & ~filters.forwarded)),
                    timeout=60,
                )
            except Exception:
                return
            f_msg_id = await get_db_message_id(client, first_message, DB_CHANNEL)
            if f_msg_id:
                break
            await first_message.reply("❌ Not from my DB Channel.")

        while True:
            try:
                second_message = await client.ask(
                    chat_id=uid,
                    text=(
                        "Forward the <b>Last</b> message from DB Channel (with Quotes)\n"
                        "or send its post link:"
                    ),
                    filters=(filters.forwarded | (filters.text & ~filters.forwarded)),
                    timeout=60,
                )
            except Exception:
                return
            s_msg_id = await get_db_message_id(client, second_message, DB_CHANNEL)
            if s_msg_id:
                break
            await second_message.reply("❌ Not from my DB Channel.")

        username = await _get_bot_username(client)
        base64_string = await encode(
            f"get-{f_msg_id * abs(DB_CHANNEL)}-{s_msg_id * abs(DB_CHANNEL)}"
        )
        link = f"https://t.me/{username}?start={base64_string}"
        await second_message.reply_text(
            f"<b>Here is your batch link</b>\n\n{link}",
            quote=True,
            reply_markup=_make_link_markup(link),
        )
    finally:
        _main_module.fwd_active_users.discard(uid)


# ── /start {base64} ───────────────────────────────────────────────────────────

@Bot.on_message(filters.private & filters.regex(r'^/start \S+'))
async def start_with_link(client: Client, message: Message):
    if not DB_CHANNEL:
        return await message.reply("❌ Bot is not configured yet.")

    try:
        base64_string = message.text.split(" ", 1)[1]
    except IndexError:
        return

    try:
        string = await decode(base64_string)
    except Exception:
        return await message.reply("❌ Invalid link.")

    argument = string.split("-")
    ids = []

    if len(argument) == 3 and argument[0] == "get":
        try:
            start = int(int(argument[1]) / abs(DB_CHANNEL))
            end = int(int(argument[2]) / abs(DB_CHANNEL))
        except Exception:
            return
        ids = list(range(start, end + 1)) if start <= end else list(range(start, end - 1, -1))
    elif len(argument) == 2 and argument[0] == "get":
        try:
            ids = [int(int(argument[1]) / abs(DB_CHANNEL))]
        except Exception:
            return
    else:
        return

    temp_msg = await message.reply("⏳ Please wait…")
    try:
        messages = await get_db_messages(client, DB_CHANNEL, ids)
    except Exception:
        await message.reply_text("❌ Something went wrong retrieving files.")
        return
    await temp_msg.delete()

    for msg in messages:
        caption = ""
        if CUSTOM_CAPTION and msg.document:
            try:
                caption = CUSTOM_CAPTION.format(
                    previouscaption=msg.caption.html if msg.caption else "",
                    filename=msg.document.file_name,
                )
            except Exception:
                caption = msg.caption.html if msg.caption else ""
        else:
            caption = msg.caption.html if msg.caption else ""

        reply_markup = msg.reply_markup if DISABLE_CHANNEL_BUTTON else None
        try:
            await msg.copy(
                chat_id=message.from_user.id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                protect_content=PROTECT_CONTENT,
                reply_markup=reply_markup,
            )
            await asyncio.sleep(0.5)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await msg.copy(
                    chat_id=message.from_user.id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    protect_content=PROTECT_CONTENT,
                    reply_markup=reply_markup,
                )
            except Exception:
                pass
        except Exception:
            pass
