import logging
import os
import sys
import asyncio
import json
import re

from .. import bot as gagan
from .. import userbot, Bot, API_ID, API_HASH

from main.plugins.pyroplug import get_bulk_msg
from main.plugins.helpers import get_link

from telethon import events, Button
from pyrogram import Client
from pyrogram.errors import FloodWait


# ── Per-user session helper ───────────────────────────────────────────────────

def _get_user_session(user_id):
    if os.path.exists("user_sessions.json"):
        try:
            with open("user_sessions.json") as f:
                return json.load(f).get(str(user_id))
        except Exception:
            return None
    return None


# ── Logging / stdout redirect ─────────────────────────────────────────────────

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

temp_log_file = "logs.txt"

if not os.path.exists(temp_log_file):
    with open(temp_log_file, "w"):
        pass


class StreamToLogger:
    def __init__(self, lg, level, path):
        self.logger = lg
        self.log_level = level
        self.log_file = path

    def write(self, buf):
        with open(self.log_file, 'a') as f:
            f.write(buf)
        for line in buf.rstrip().splitlines():
            self.logger.log(self.log_level, line.rstrip())

    def flush(self):
        pass

    def fileno(self):
        return 0


for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(filename=temp_log_file, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

sys.stdout = StreamToLogger(logging.getLogger('STDOUT'), logging.INFO, temp_log_file)
sys.stderr = StreamToLogger(logging.getLogger('STDERR'), logging.ERROR, temp_log_file)


def _reset_log():
    try:
        open(temp_log_file, "w").close()
    except Exception:
        pass


async def _log_loop():
    while True:
        await asyncio.sleep(180)
        _reset_log()


asyncio.ensure_future(_log_loop())


# ── /logs ─────────────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern='/logs'))
async def send_log(event):
    if os.path.exists(temp_log_file):
        await gagan.send_file(event.sender_id, temp_log_file,
                              caption="Log file (last 3 min).")
    else:
        await event.respond("Log file not found.")


# ── active batch tracker ──────────────────────────────────────────────────────
# user_id → False = running, True = cancelled

active_batches = {}


# ── /cancel ───────────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern='/cancel'))
async def cancel_command(event):
    uid = event.sender_id
    if active_batches.get(uid) is False:
        active_batches[uid] = True
        await event.respond("✅ Batch cancelled.")
    else:
        await event.respond("There is no running batch to cancel.")


# ── Link parser ───────────────────────────────────────────────────────────────

def _parse_link(link: str):
    """
    Parse any Telegram link whose last segment is START, or START-END.

    Supports:
      t.me/c/CHATID/MSGID
      t.me/c/CHATID/MSGID-ENDMSGID
      t.me/c/CHATID/TOPICID/MSGID
      t.me/c/CHATID/TOPICID/MSGID-ENDMSGID
      t.me/USERNAME/MSGID
      t.me/USERNAME/MSGID-ENDMSGID

    Returns (chat_ref, start_msg, end_msg) or None.
      chat_ref: int (private, with -100) or str (public username)
    """
    clean = link.rstrip("/").split("?")[0]
    parts = clean.split("/")
    # parts[0]='https:', [1]='', [2]='t.me', [3]='c' or username, ...

    if len(parts) < 5:
        return None

    last = parts[-1]
    if "-" in last:
        segs = last.split("-", 1)
        try:
            start_msg = int(segs[0])
            end_msg   = int(segs[1])
        except ValueError:
            return None
    else:
        try:
            start_msg = int(last)
            end_msg   = start_msg
        except ValueError:
            return None

    if "t.me/c/" in link:
        # Private: parts[4] = CHATID, parts[5] = TOPICID or MSGID
        try:
            chat_ref = int("-100" + parts[4])
        except (ValueError, IndexError):
            return None
        return chat_ref, start_msg, end_msg
    else:
        # Public: parts[3] = username (index before the message segment)
        username = parts[-2]
        if not username or username.lower() in ("c", "t.me", "telegram.me"):
            return None
        return username, start_msg, end_msg


# ── /batch ────────────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern='/batch'))
async def _bulk(event):
    uid = event.sender_id

    if active_batches.get(uid) is False:
        return await event.reply("A batch is already running. Use /cancel to stop it first.")

    # Safe defaults
    parsed   = None
    chat_ref = None
    start_msg = end_msg = 0

    async with gagan.conversation(event.chat_id, timeout=120) as conv:
        try:
            await conv.send_message(
                "Send the message link with a **start–end range**.\n\n"
                "**Examples:**\n"
                "• `https://t.me/c/2133410746/926447-926450` — private channel\n"
                "• `https://t.me/c/3765531856/4/23-25` — supergroup topic\n"
                "• `https://t.me/username/100-120` — public channel\n\n"
                "_For a single file, just send the normal link without a range._",
                buttons=Button.force_reply()
            )
            link_msg = await conv.get_reply()

            raw = link_msg.text.strip() if link_msg.text else ""
            _link = get_link(raw) or raw

            if not _link:
                await conv.send_message("No valid link found. Please try /batch again.")
                return

            parsed = _parse_link(_link)
            if not parsed:
                await conv.send_message(
                    "❌ Could not read a message range from that link.\n"
                    "Make sure it ends like `…/START-END` (e.g. `…/100-150`)."
                )
                return

            chat_ref, start_msg, end_msg = parsed
            total = end_msg - start_msg + 1

            if total < 1:
                await conv.send_message("End ID must be ≥ start ID.")
                return
            if total > 10000:
                await conv.send_message("Max range is 10 000 messages per batch.")
                return

            active_batches[uid] = False   # mark as running
            await conv.send_message(
                f"🚀 **Batch starting**\n"
                f"Chat: `{chat_ref}`\n"
                f"Range: `{start_msg}` → `{end_msg}` ({total} messages)\n\n"
                "Use /cancel to stop."
            )

        except asyncio.TimeoutError:
            await event.respond("⏳ Timed out. Please try /batch again.")
            return
        except Exception as e:
            logger.info(e)
            await event.respond(f"Error: {e}")
            return

    # ── Resolve account ───────────────────────────────────────────────────────
    acc = userbot
    personal_acc = None

    if acc is None:
        sess = _get_user_session(uid)
        if sess:
            try:
                personal_acc = Client(
                    f"batch_{uid}",
                    session_string=sess,
                    api_id=int(API_ID),
                    api_hash=API_HASH,
                    in_memory=True
                )
                await personal_acc.start()
                acc = personal_acc
            except Exception as e:
                await Bot.send_message(uid, f"⚠️ Could not start your session: `{e}`")
                personal_acc = None

    # For private channels we NEED a user account.
    # For public channels the bot can copy directly — acc may be None.
    if acc is None and isinstance(chat_ref, int):
        active_batches.pop(uid, None)
        return await Bot.send_message(
            uid,
            "❌ **No user session available.**\n\n"
            "A Telegram user account is required to access private/restricted channels.\n"
            "👉 Use /login to authenticate, then try /batch again."
        )

    # ── Run ───────────────────────────────────────────────────────────────────
    try:
        await _run_batch(acc, Bot, uid, chat_ref, start_msg, end_msg)
    finally:
        if personal_acc:
            try:
                await personal_acc.stop()
            except Exception:
                pass
        active_batches.pop(uid, None)


# ── Batch runner ──────────────────────────────────────────────────────────────

async def _run_batch(acc, client, sender, chat_ref, start_msg: int, end_msg: int):
    total    = end_msg - start_msg + 1
    saved    = 0
    skipped  = 0

    status = await client.send_message(
        sender,
        f"🔍 **Batch in progress**\n"
        f"Range: `{start_msg}` → `{end_msg}` ({total} IDs)\n\n"
        f"⏳ Starting..."
    )

    for msg_id in range(start_msg, end_msg + 1):

        # Check for cancellation
        if active_batches.get(sender):
            await client.send_message(sender, "✅ Batch cancelled.")
            return

        # Live status update
        try:
            await status.edit(
                f"🔄 **Batch in progress**\n"
                f"Range: `{start_msg}` → `{end_msg}`\n\n"
                f"📌 **Now:** `{msg_id}`\n"
                f"✅ Saved: `{saved}` | ⏭️ Skipped: `{skipped}`"
            )
        except Exception:
            pass

        # Build link for this message ID
        if isinstance(chat_ref, int):
            raw_id   = str(chat_ref).replace("-100", "")
            link_str = f"https://t.me/c/{raw_id}/{msg_id}"
        else:
            link_str = f"https://t.me/{chat_ref}/{msg_id}"

        try:
            await get_bulk_msg(acc, client, sender, link_str, msg_id)
            saved += 1
        except FloodWait as fw:
            fw_val = int(fw.value) if hasattr(fw, 'value') else int(fw.x)
            if fw_val > 299:
                await client.send_message(sender, f"⏳ FloodWait > 5 min ({fw_val}s). Stopping batch.")
                return
            alert = await client.send_message(sender, f"⏳ FloodWait {fw_val}s, waiting...")
            await asyncio.sleep(fw_val + 3)
            await alert.delete()
            try:
                await get_bulk_msg(acc, client, sender, link_str, msg_id)
                saved += 1
            except Exception as e:
                logger.info(f"Retry error msg {msg_id}: {e}")
                skipped += 1
        except Exception as e:
            logger.info(f"Error msg {msg_id}: {e}")
            skipped += 1

        # Sleep between messages (except after the last one)
        if msg_id < end_msg:
            wait = 3 if saved < 25 else (5 if saved < 50 else 8)
            await asyncio.sleep(wait)

    # Done
    try:
        await status.edit(
            f"✅ **Batch complete!**\n"
            f"Range: `{start_msg}` → `{end_msg}`\n\n"
            f"📦 **Saved:** `{saved}`\n"
            f"⏭️ **Skipped** (empty / missing): `{skipped}`"
        )
    except Exception:
        await client.send_message(
            sender,
            f"✅ Batch done! Saved: {saved} | Skipped: {skipped}"
        )
