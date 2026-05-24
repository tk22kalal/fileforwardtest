#Join me at telegram @dev_gagan

import os

from pyrogram import Client

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

import logging, time, sys
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ── Credentials: env vars first, hardcoded fallback ──────────────────────────
API_ID    = int(os.environ.get("API_ID",    "24058425"))
API_HASH  = os.environ.get("API_HASH",      "694b063e55c24287a3d30aed90191373")
BOT_TOKEN = os.environ.get("BOT_TOKEN",     "8600580531:AAFnpo9I-3e2PH9NnpfEy0KG3i8_zJMLR90")
SESSION   = os.environ.get("SESSION",       "").strip()
FORCESUB  = os.environ.get("FORCESUB",      "forcesubpavo3")
AUTH      = os.environ.get("AUTH",          "7390527029")

SUDO_USERS = set()
if AUTH.strip():
    SUDO_USERS = {int(x.strip()) for x in AUTH.split()}

# ── Telethon bot (always required) ────────────────────────────────────────────
bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ── Pyrogram userbot (optional — users can /login instead) ───────────────────
userbot = None
if SESSION:
    try:
        userbot = Client("myacc", api_id=API_ID, api_hash=API_HASH,
                         session_string=SESSION)
        userbot.start()
        print("Global userbot started successfully.")
    except BaseException as e:
        print(f"Warning: Could not start global userbot: {e}")
        print("SESSION env var may be invalid or expired.")
        print("Users can authenticate via /login instead.")
        userbot = None
else:
    print("No SESSION provided — global userbot disabled.")
    print("Users must use /login to access restricted content.")

# ── Pyrogram bot (always required) ────────────────────────────────────────────
Bot = Client(
    "SaveRestricted",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH
)

try:
    Bot.start()
except Exception as e:
    print(f"Fatal: Could not start Bot client: {e}")
    sys.exit(1)
