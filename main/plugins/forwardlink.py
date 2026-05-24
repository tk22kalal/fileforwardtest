import asyncio
import re
import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import Bot, userbot, SUDO_USERS, DB_CHANNEL, PROTECT_CONTENT, CUSTOM_CAPTION, DISABLE_CHANNEL_BUTTON
from main.plugins.link_helpers import encode, decode, get_db_messages, get_db_message_id

logger = logging.getLogger(__name__)

ADMINS = list(SUDO_USERS)

_bot_username_cache = None


async def _get_bot_username(client: Client) -> str:
    global _bot_username_cache
    if not _bot_username_cache:
        me = await client.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


def _parse_tme_link(link: str):
    """
    Parse a Telegram link into (chat_id, msg_id).
    chat_id is int (-100XXXXXX) for private channels, str username for public.
    Returns (None, None) on failure.
    """
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


# ── /fwd — forward single restricted message → DB channel → share link ────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("fwd"))
async def fwd_command(client: Client, message: Message):
    if not userbot:
        return await message.reply("❌ No userbot SESSION configured.\nSet the SESSION env var and restart.")
    if not DB_CHANNEL:
        return await message.reply("❌ DB_CHANNEL not configured.\nSet the DB_CHANNEL env var and restart.")

    try:
        asked = await client.ask(
            chat_id=message.from_user.id,
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
        sent = await userbot.copy_message(
            chat_id=DB_CHANNEL,
            from_chat_id=src_chat,
            message_id=msg_id,
        )
    except Exception as e:
        return await status.edit(f"❌ Failed to copy message:\n<code>{e}</code>")

    username = await _get_bot_username(client)
    base64_string = await encode(f"get-{sent.id * abs(DB_CHANNEL)}")
    link = f"https://t.me/{username}?start={base64_string}"
    await status.edit(
        f"✅ <b>Done! Here is your share link:</b>\n\n{link}",
        reply_markup=_make_link_markup(link),
    )


# ── /batchfwd — forward a range of restricted messages → DB channel → share link

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("batchfwd"))
async def batchfwd_command(client: Client, message: Message):
    if not userbot:
        return await message.reply("❌ No userbot SESSION configured.\nSet the SESSION env var and restart.")
    if not DB_CHANNEL:
        return await message.reply("❌ DB_CHANNEL not configured.\nSet the DB_CHANNEL env var and restart.")

    while True:
        try:
            first_msg = await client.ask(
                chat_id=message.from_user.id,
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
                chat_id=message.from_user.id,
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
            sent = await userbot.copy_message(
                chat_id=DB_CHANNEL,
                from_chat_id=src_chat,
                message_id=msg_id,
            )
            if db_start_id is None:
                db_start_id = sent.id
            db_end_id = sent.id
            done += 1
        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
            try:
                sent = await userbot.copy_message(
                    chat_id=DB_CHANNEL,
                    from_chat_id=src_chat,
                    message_id=msg_id,
                )
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


# ── /genlink — generate share link for a message already in DB channel ────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("genlink"))
async def genlink_command(client: Client, message: Message):
    if not DB_CHANNEL:
        return await message.reply("❌ DB_CHANNEL not configured.")

    while True:
        try:
            channel_message = await client.ask(
                chat_id=message.from_user.id,
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


# ── /mkbatch — generate batch link for a range already in DB channel ──────────

@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("mkbatch"))
async def mkbatch_command(client: Client, message: Message):
    if not DB_CHANNEL:
        return await message.reply("❌ DB_CHANNEL not configured.")

    while True:
        try:
            first_message = await client.ask(
                chat_id=message.from_user.id,
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
                chat_id=message.from_user.id,
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


# ── /start {base64} — serve files from DB channel to users ────────────────────
# Only fires when /start has a parameter (avoids conflict with Telethon /start)

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
