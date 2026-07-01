from typing import Any

from aiohttp import web

POSTGRES_POOL_KEY: web.AppKey = web.AppKey("postgres_pool")
SESSION_STATE_KEY: web.AppKey = web.AppKey("session_state")
CONVERSATION_STATE_KEY: web.AppKey = web.AppKey("conversation_state")


def get_user_session(app: web.Application, user_id: str) -> dict[str, Any]:
    sessions = app[SESSION_STATE_KEY]
    if user_id not in sessions:
        sessions[user_id] = {
            "face_id": None,
            "face_name": None,
            "model": None,
            "backend": None,
        }
    return sessions[user_id]


def get_conversation_session(
    app: web.Application, user_id: str, conversation_id: str
) -> dict[str, Any]:
    sessions = app[CONVERSATION_STATE_KEY]
    key = f"{user_id}:{conversation_id}"
    if key not in sessions:
        sessions[key] = {
            "face_id": None,
            "face_name": None,
            "model": None,
            "backend": None,
        }
    return sessions[key]
