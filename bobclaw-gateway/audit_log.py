"""
BoBClaw Gateway — Structured request audit logging middleware

Emits one JSON line per request to logger ``bobclaw.gateway.audit``.
Outermost middleware: captures every request including auth failures
(401) and rate-limit rejections (429). No payload logging — only
request metadata, never bodies or headers.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("bobclaw.gateway.audit")


def _user_id(request: web.Request) -> str | None:
    user = request.get("user")
    if user and isinstance(user, dict):
        return user.get("sub")
    return None


def _emit(record: dict) -> None:
    """Write the audit record as a single JSON line via stdlib logging."""
    logger.info(json.dumps(record, separators=(",", ":")))


def make_audit_log_middleware(enabled: bool = True) -> Callable[
    [web.Request, Callable], Awaitable[web.StreamResponse]
]:
    """Factory: build an aiohttp audit-log middleware. ``enabled=False``
    returns a pass-through that adds no headers and emits no logs."""

    @web.middleware
    async def audit_log_middleware(request: web.Request, handler):
        if not enabled:
            return await handler(request)

        request_id = uuid.uuid4().hex
        request["request_id"] = request_id
        start = time.monotonic()
        status = 0
        error_class: str | None = None

        try:
            response = await handler(request)
            status = response.status
            try:
                response.headers["X-Request-ID"] = request_id
            except Exception:
                # Some StreamResponses are already prepared and reject
                # header mutation; non-fatal for audit purposes.
                pass
            return response
        except web.HTTPException as exc:
            status = exc.status
            error_class = type(exc).__name__
            raise
        except Exception as exc:  # noqa: BLE001
            status = 500
            error_class = type(exc).__name__
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "user": _user_id(request),
                "method": request.method,
                "path": request.path,
                "status": status,
                "duration_ms": duration_ms,
                "remote": request.remote,
            }
            if error_class is not None:
                record["error_class"] = error_class
            _emit(record)

    return audit_log_middleware
