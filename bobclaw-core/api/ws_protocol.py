"""
BoBClaw Core — stream-event protocol

Event types and builder helpers for the core's streaming surface.  Both
``/api/chat`` (SSE) and the gateway's ``/ws/chat`` speak the same event
shape; keeping builders in one place prevents drift between services.

The contract is consumed by ``bobclaw-gateway/routers/chat.py``
(see ``_parse_stream_event`` there); any new event type must land in both
files together.
"""
from __future__ import annotations

from typing import Any, Optional

# ─── Event type names ─────────────────────────────────────────────────────────
CHUNK = "chunk"
MESSAGE_COMPLETE = "message_complete"
APPROVAL_REQUEST = "approval_request"
ERROR = "error"


# ─── Builders ─────────────────────────────────────────────────────────────────

def chunk_event(
    content: str,
    model: Optional[str] = None,
    backend: Optional[str] = None,
) -> dict[str, Any]:
    """One streamed delta of assistant text."""
    return {
        "type": CHUNK,
        "content": content,
        "model": model,
        "backend": backend,
    }


def message_complete_event(
    *,
    message_id: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    elapsed_ms: int = 0,
    model: Optional[str] = None,
    backend: Optional[str] = None,
) -> dict[str, Any]:
    """Terminal event for a successful assistant turn."""
    return {
        "type": MESSAGE_COMPLETE,
        "message_id": message_id,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "elapsed_ms": elapsed_ms,
        "model": model,
        "backend": backend,
    }


def approval_request_event(
    *,
    approval_id: str,
    action: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Signals the human-in-the-loop gate to the client."""
    return {
        "type": APPROVAL_REQUEST,
        "approval_id": approval_id,
        "action": action,
        "details": details or {},
    }


def error_event(message: str, code: str = "internal_error") -> dict[str, Any]:
    return {"type": ERROR, "message": message, "code": code}
