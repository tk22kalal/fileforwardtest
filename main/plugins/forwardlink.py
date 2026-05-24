import asyncio
import re
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


def _userbot():
    """Always return the current live userbot from the main module."""
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


async def _copy_msg_to_db(ub: Client, db_channel: int, src_chat, msg_id: int):
    """
    Copy a single message to the DB channel.

    Strategy:
      1. Try server-side copy_message (fast, zero bandwidth — works when
         the channel does NOT have forward-restrictions).
      2. Fall back to in-memory download → re-upload (no temp files on disk).
         This is the only way to handle channels with noforwards=True.
    """
    # ── Attempt 1: server-side copy ───────────────────────────────────────────
    try:
        return await ub.copy_message(
            chat_id=db_channel,
            from_chat_id=src_chat,
            message_id=msg_id,
        )
    except Exception as e:
        if "CHAT_FORWARDS_RESTRICTED" not in str(e):
            raise  # unexpected error — let the caller handle it
        logger.info(f"Server-side copy blocked (noforwards). Falling back to in-memory. msg={msg_id}")

    # ── Attempt 2: in-memory download → re-upload ─────────────────────────────
    msgs = await ub.get_messages(src_chat, msg_id)
    if not msgs:
        raise RuntimeError("Message not found in source channel")

    caption = (msgs.caption or msgs.text or "")
    if hasattr(caption, "html"):
        caption = caption.html

    if not msgs.media:
        return await ub.send_message(db_channel, caption or "(empty message)")

    bio = await ub.download_media(msgs, in_memory=True)
    bio.seek(0)

    if msgs.video:
        bio.name = getattr(msgs.video, "file_name", None) or "video.mp4"
        return await ub.send_video(
            db_channel, bio,
            caption=caption,
            duration=msgs.video.duration,
            width=msgs.video.width,
            height=msgs.video.height,
        )
    elif msgs.document:
        bio.name = getattr(msgs.document, "file_name", None) or "document"
        return await ub.send_document(db_channel, bio, caption=caption, file_name=bio.name)
    elif msgs.audio:
        bio.name = getattr(msgs.audio, "file_name", None) or "audio.mp3"
        return await ub.send_audio(db_channel, bio, caption=caption)
    elif msgs.photo:
        bio.name = "photo.jpg"
        return await ub.send_photo(db_channel, bio, caption=caption)
    else:
        bio.name = "file"
        return await ub.send_document(db_channel, bio, caption=caption)


# ── /fwd — forward single restricted message → DB channel → share link ────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("fwd"))
async def fwd_command(client: Client, message: Message):
    ub = _userbot()
    if not ub:
        return await message.reply(_userbot_error())
    if not DB_CHANNEL:
        return await message.reply(
            "❌ <b>DB_CHANNEL not set.</b>\n\n"
            "Add your database channel ID as the <code>DB_CHANNEL</code> env var and redeploy."
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
            sent = await _copy_msg_to_db(ub, DB_CHANNEL, src_chat, msg_id)
        except Exception as e:
            return await status.edit(f"❌ Failed to copy message:\n<code>{e}</code>")

        username = await _get_bot_username(client)
        base64_string = await encode(f"get-{sent.id * abs(DB_CHANNEL)}")
        link = f"https://t.me/{username}?start={base64_string}"
        await status.edit(
            f"✅ <b>Done! Here is your share link:</b>\n\n{link}",
            reply_markup=_make_link_markup(link),
        )
    finally:
        _main_module.fwd_active_users.discard(uid)


# ── /batchfwd — forward a range of restricted messages → DB channel → share link

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("batchfwd"))
async def batchfwd_command(client: Client, message: Message):
    ub = _userbot()
    if not ub:
        return await message.reply(_userbot_error())
    if not DB_CHANNEL:
        return await message.reply(
            "❌ <b>DB_CHANNEL not set.</b>\n\n"
            "Add your database channel ID as the <code>DB_CHANNEL</code> env var and redeploy."
        )

    uid = message.from_user.id
    _main_module.fwd_active_users.add(uid)
    try:
        while True:
            try:
                first_msg = await client.ask(
                    chat_id=uid,
                    text="📎 Send the link of the <b>FIRST</b> message from the restricted channel:",
                    filters=filters.text & filters.private,
                    timeout=60,
                )
            except Exception:
                return
            src_chat, f_id = _parse_tme_link(first_msg.text.strip())
            if src_chat and f_id:
                break
            await first_msg.reply("❌ Invalid link. Please try again.")

        while True:
            try:
                last_msg = await client.ask(
                    chat_id=uid,
                    text="📎 Send the link of the <b>LAST</b> message from the restricted channel:",
                    filters=filters.text & filters.private,
                    timeout=60,
                )
            except Exception:
                return
            last_chat, l_id = _parse_tme_link(last_msg.text.strip())
            if last_chat and l_id:
                break
            await last_msg.reply("❌ Invalid link. Please try again.")

        if str(src_chat) != str(last_chat):
            return await last_msg.reply("❌ Both links must be from the same channel.")

        if f_id > l_id:
            f_id, l_id = l_id, f_id

        ids = list(range(f_id, l_id + 1))
        total = len(ids)
        status = await last_msg.reply(f"⏳ Copying {total} message(s) to DB channel…")

        db_start_id = None
        db_end_id = None
        done = 0
        failed = 0

        for msg_id in ids:
            try:
                sent = await _copy_msg_to_db(ub, DB_CHANNEL, src_chat, msg_id)
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

            if done % 10 == 0 and done > 0:
                try:
                    await status.edit(f"⏳ Copied {done}/{total}…")
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        if db_start_id is None:
            return await status.edit("❌ No messages were copied to DB channel.")

        username = await _get_bot_username(client)
        base64_string = await encode(
            f"get-{db_start_id * abs(DB_CHANNEL)}-{db_end_id * abs(DB_CHANNEL)}"
        )
        link = f"https://t.me/{username}?start={base64_string}"
        await status.edit(
            f"✅ <b>Done!</b> Copied {done}/{total} messages (skipped {failed}).\n\n"
            f"<b>Share link:</b>\n{link}",
            reply_markup=_make_link_markup(link),
        )
    finally:
        _main_module.fwd_active_users.discard(uid)


# ── /genlink — generate share link for a message already in DB channel ────────

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
                "❌ That message is not from my DB Channel.\n"
                "Forward from the correct DB channel or send its post link."
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


# ── /mkbatch — generate batch link for a range already in DB channel ──────────

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
            await first_message.reply("❌ That message is not from my DB Channel.")

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
            await second_message.reply("❌ That message is not from my DB Channel.")

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


# ── /start {base64} — serve files from DB channel to users ────────────────────

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
        if start <= end:
            ids = list(range(start, end + 1))
        else:
            i = start
            while i >= end:
                ids.append(i)
                i -= 1
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
