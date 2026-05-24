"""
Standalone session storage — no Telegram client imports.
Both batch.py and login.py can safely import from here.
"""
import json
import os

SESSIONS_FILE = "user_sessions.json"


def _load_sessions() -> dict:
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_sessions(data: dict):
    with open(SESSIONS_FILE, "w") as f:
        json.dump(data, f)


def get_user_session(user_id) -> str | None:
    """Return the stored session string for user_id, or None if not found."""
    sessions = _load_sessions()
    return sessions.get(str(user_id))


def store_session(user_id, session_string: str):
    sessions = _load_sessions()
    sessions[str(user_id)] = session_string
    _save_sessions(sessions)


def remove_session(user_id):
    sessions = _load_sessions()
    sessions.pop(str(user_id), None)
    _save_sessions(sessions)

