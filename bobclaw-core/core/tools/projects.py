"""
BoBClaw Core — Project-action tools

P2 adds a native ``create_project`` tool. Identity is supplied per-turn via
contextvars set by ``execute_node`` before the tool loop runs.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Optional

from langchain_core.tools import tool

from core.db import get_pool

logger = logging.getLogger(__name__)

# Per-turn identity, set by execute_node around the tool loop. Contextvars keep
# the scope limited to the running turn and avoid threading state through every
# LangChain tool signature.
_current_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_user_id", default=None
)
_current_conversation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_conversation_id", default=None
)


@tool
async def create_project(
    name: str,
    description: str = "",
    instructions: str = "",
    default_face: Optional[str] = None,
    default_backend: Optional[str] = None,
) -> str:
    """Create a new BoBClaw project and assign the current conversation to it.

    Requires ``_current_user_id`` to be set; returns an error string if identity
    is missing (fail-closed). The current conversation is updated to point at
    the new project when ``_current_conversation_id`` is available.
    """
    user_id = _current_user_id.get()
    conversation_id = _current_conversation_id.get()

    if not user_id:
        return "Error: user_id is required to create a project"

    clean_name = name.strip()
    if not clean_name:
        return "Error: project name is required"

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO projects (
                    user_id, name, description, instructions,
                    default_face_id, default_backend
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, name
                """,
                user_id,
                clean_name,
                description or None,
                instructions or None,
                default_face,
                default_backend,
            )
            project_id = row["id"]
            if conversation_id:
                try:
                    await conn.execute(
                        """
                        UPDATE conversations
                        SET project_id = $1, updated_at = NOW()
                        WHERE id = $2 AND user_id = $3
                        """,
                        project_id,
                        conversation_id,
                        user_id,
                    )
                except Exception as exc:
                    # The project row is already committed in the same
                    # transaction; surfacing the assignment failure lets the
                    # model report it without rolling back the create.
                    logger.warning(
                        "Failed to assign conversation %s to project %s: %s",
                        conversation_id, project_id, exc,
                    )
                    return (
                        f"Created project '{row['name']}' ({project_id}), "
                        f"but could not assign the conversation: {exc}"
                    )

    return f"Created project '{row['name']}' ({project_id})."
