import asyncio
import base64

from pyrogram.errors import FloodWait


async def encode(string: str) -> str:
    string_bytes = string.encode("ascii")
    base64_bytes = base64.urlsafe_b64encode(string_bytes)
    return (base64_bytes.decode("ascii")).strip("=")


async def decode(base64_string: str) -> str:
    base64_string = base64_string.strip("=")
    base64_bytes = (base64_string + "=" * (-len(base64_string) % 4)).encode("ascii")
    string_bytes = base64.urlsafe_b64decode(base64_bytes)
    return string_bytes.decode("ascii")


async def get_db_messages(client, db_channel_id: int, message_ids: list) -> list:
    messages = []
    total = 0
    while total < len(message_ids):
        batch = message_ids[total: total + 200]
        try:
            msgs = await client.get_messages(chat_id=db_channel_id, message_ids=batch)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            msgs = await client.get_messages(chat_id=db_channel_id, message_ids=batch)
        except Exception:
            break
        total += len(batch)
        messages.extend(msgs)
    return messages


async def get_db_message_id(client, message, db_channel_id: int, db_channel_username: str = None) -> int:
    if (
        message.forward_from_chat
        and message.forward_from_chat.id == db_channel_id
    ):
        return message.forward_from_message_id

    if message.forward_from_chat or message.forward_sender_name or not message.text:
        return 0

    import re
    matches = re.match(r"https://t\.me/(?:c/)?(.+)/(\d+)", message.text.strip())
    if not matches:
        return 0

    channel_id = matches.group(1)
    msg_id = int(matches.group(2))

    if channel_id.isdigit():
        if f"-100{channel_id}" == str(db_channel_id):
            return msg_id
    elif db_channel_username and channel_id == db_channel_username:
        return msg_id

    return 0

