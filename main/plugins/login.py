import asyncio
import logging

from telethon import events, Button
from pyrogram import Client
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid, FloodWait
)

from .. import bot as gagan, API_ID, API_HASH

_SESSIONS_FILE = "user_sessions.json"


def get_user_session(user_id):
    import json, os
    if os.path.exists(_SESSIONS_FILE):
        try:
            with open(_SESSIONS_FILE, "r") as f:
                return json.load(f).get(str(user_id))
        except Exception:
            return None
    return None


def store_session(user_id, session_string: str):
    import json, os
    data = {}
    if os.path.exists(_SESSIONS_FILE):
        try:
            with open(_SESSIONS_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data[str(user_id)] = session_string
    with open(_SESSIONS_FILE, "w") as f:
        json.dump(data, f)


def remove_session(user_id):
    import json, os
    if not os.path.exists(_SESSIONS_FILE):
        return
    try:
        with open(_SESSIONS_FILE, "r") as f:
            data = json.load(f)
        data.pop(str(user_id), None)
        with open(_SESSIONS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)


# ── /login ─────────────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern="/login"))
async def login_command(event):
    if not event.is_private:
        return await event.reply("Please use /login in private chat with the bot.")

    user_id = event.sender_id

    if get_user_session(user_id):
        return await event.reply(
            "✅ You are already logged in.\n"
            "Use /logout to log out first if you want to re-login."
        )

    async with gagan.conversation(event.chat_id, timeout=180) as conv:
        await conv.send_message(
            "📱 **Login with your Telegram account**\n\n"
            "Send your phone number with country code.\n"
            "Example: `+919876543210`",
            buttons=Button.force_reply()
        )
        try:
            phone_msg = await conv.get_reply()
        except asyncio.TimeoutError:
            return await conv.send_message("⏰ Timed out. Please try /login again.")

        phone = phone_msg.text.strip() if phone_msg.text else ""
        if not phone.startswith("+"):
            return await conv.send_message(
                "❌ Invalid phone number. Must start with +. Try /login again."
            )

        tmp_client = Client(
            f"login_tmp_{user_id}",
            api_id=int(API_ID),
            api_hash=API_HASH,
            in_memory=True
        )

        try:
            await tmp_client.connect()
        except Exception as e:
            return await conv.send_message(f"❌ Could not connect: `{e}`")

        try:
            sent = await tmp_client.send_code(phone)
        except PhoneNumberInvalid:
            await tmp_client.disconnect()
            return await conv.send_message("❌ Invalid phone number. Try /login again.")
        except FloodWait as fw:
            await tmp_client.disconnect()
            return await conv.send_message(
                f"⏳ FloodWait: please try again after {fw.value} seconds."
            )
        except Exception as e:
            await tmp_client.disconnect()
            return await conv.send_message(f"❌ Error sending code: `{e}`")

        await conv.send_message(
            "✅ OTP sent to your Telegram account.\n\n"
            "Enter the OTP now (digits only, e.g. `12345`).\n"
            "⚠️ Send it as plain text — do NOT use spaces.",
            buttons=Button.force_reply()
        )
        try:
            otp_msg = await conv.get_reply()
        except asyncio.TimeoutError:
            await tmp_client.disconnect()
            return await conv.send_message("⏰ Timed out waiting for OTP.")

        otp = otp_msg.text.strip().replace(" ", "") if otp_msg.text else ""

        try:
            await tmp_client.sign_in(phone, sent.phone_code_hash, otp)
        except PhoneCodeInvalid:
            await tmp_client.disconnect()
            return await conv.send_message("❌ Wrong OTP. Please try /login again.")
        except PhoneCodeExpired:
            await tmp_client.disconnect()
            return await conv.send_message("❌ OTP expired. Please try /login again.")
        except SessionPasswordNeeded:
            await conv.send_message(
                "🔐 Your account has **Two-Step Verification** enabled.\n"
                "Send your 2FA password:",
                buttons=Button.force_reply()
            )
            try:
                pwd_msg = await conv.get_reply()
            except asyncio.TimeoutError:
                await tmp_client.disconnect()
                return await conv.send_message("⏰ Timed out waiting for 2FA password.")

            password = pwd_msg.text.strip() if pwd_msg.text else ""
            try:
                await tmp_client.check_password(password)
            except PasswordHashInvalid:
                await tmp_client.disconnect()
                return await conv.send_message("❌ Wrong 2FA password. Try /login again.")
            except Exception as e:
                await tmp_client.disconnect()
                return await conv.send_message(f"❌ 2FA error: `{e}`")
        except FloodWait as fw:
            await tmp_client.disconnect()
            return await conv.send_message(
                f"⏳ FloodWait {fw.value}s. Try again later."
            )
        except Exception as e:
            await tmp_client.disconnect()
            return await conv.send_message(f"❌ Sign-in error: `{e}`")

        try:
            session_string = await tmp_client.export_session_string()
            await tmp_client.disconnect()
        except Exception as e:
            await tmp_client.disconnect()
            return await conv.send_message(f"❌ Could not export session: `{e}`")

        store_session(user_id, session_string)
        logger.info(f"User {user_id} logged in successfully.")
        await conv.send_message(
            "✅ **Logged in successfully!**\n\n"
            "Your session is saved. You can now use /batch with your personal account.\n"
            "Use /logout to remove your session anytime."
        )


# ── /logout ────────────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern="/logout"))
async def logout_command(event):
    if not event.is_private:
        return await event.reply("Please use /logout in private chat.")

    user_id = event.sender_id
    if not get_user_session(user_id):
        return await event.reply("You are not logged in.")

    remove_session(user_id)
    logger.info(f"User {user_id} logged out.")
    await event.reply("✅ Logged out. Your session has been removed.")


# ── /mysession ─────────────────────────────────────────────────────────────────

@gagan.on(events.NewMessage(incoming=True, pattern="/mysession"))
async def mysession_command(event):
    if not event.is_private:
        return
    user_id = event.sender_id
    if get_user_session(user_id):
        await event.reply("✅ You have an active session stored.")
    else:
        await event.reply("❌ No session found. Use /login to log in.")
