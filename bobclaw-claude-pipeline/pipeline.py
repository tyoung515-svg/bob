"""
BoBClaw Claude Build Pipeline — Main aiohttp Service

Endpoints:
  POST   /builds                    — start a new build
  GET    /builds                    — list recent builds
  GET    /builds/{session_id}       — build status
  GET    /builds/{session_id}/stream — SSE stream of progress events
  DELETE /builds/{session_id}       — cancel build
  GET    /health                    — health check
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import anthropic
import jwt
from aiohttp import web

import config
from session_manager import (
    BuildSession,
    BuildStatus,
    MaxConcurrentBuildsError,
    SessionManager,
    SessionNotFoundError,
)
from tools import TOOL_SCHEMAS, ToolExecutor

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Validate JWT Bearer tokens on all routes except /health."""
    if request.path == "/health":
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return web.json_response(
            {"error": "Missing or invalid Authorization header"}, status=401
        )

    token = auth_header[7:]
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return web.json_response({"error": "Invalid or expired token"}, status=401)

    request["user"] = payload
    return await handler(request)


def create_app(session_manager: SessionManager | None = None) -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application(middlewares=[auth_middleware])
    app["session_manager"] = session_manager or SessionManager(
        max_concurrent_builds=config.MAX_CONCURRENT_BUILDS
    )
    # SSE subscriber queues: session_id → list[asyncio.Queue]
    app["sse_subscribers"]: dict[str, list[asyncio.Queue]] = {}
    app["sse_buffers"]: dict[str, deque] = {}

    app.router.add_post("/builds", handle_create_build)
    app.router.add_get("/builds", handle_list_builds)
    app.router.add_get("/builds/{session_id}", handle_get_build)
    app.router.add_get("/builds/{session_id}/stream", handle_stream_build)
    app.router.add_delete("/builds/{session_id}", handle_cancel_build)
    app.router.add_get("/health", handle_health)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    return app


async def _on_startup(app: web.Application) -> None:
    sm: SessionManager = app["session_manager"]
    await sm.ensure_schema()
    log.info("BoBClaw pipeline started on %s:%s", config.HOST, config.PORT)


async def _on_cleanup(app: web.Application) -> None:
    log.info("BoBClaw pipeline shutting down.")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


async def handle_create_build(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Request body must be valid JSON.")

    task: str = body.get("task", "").strip()
    if not task:
        raise web.HTTPBadRequest(text="'task' is required.")

    model: str | None = body.get("model")
    if model and model not in config.ALLOWED_MODELS:
        raise web.HTTPBadRequest(
            text=f"Model '{model}' is not allowed. Allowed: {config.ALLOWED_MODELS}"
        )

    sm: SessionManager = request.app["session_manager"]

    try:
        session = await sm.create_session(task=task, model=model)
    except MaxConcurrentBuildsError as exc:
        raise web.HTTPTooManyRequests(text=str(exc))

    # Launch the build coroutine as a background task
    asyncio.create_task(
        _run_build(request.app, session),
        name=f"build-{session.id}",
    )

    return web.json_response(session.to_dict(), status=202)


async def handle_list_builds(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status")
    sm: SessionManager = request.app["session_manager"]
    sessions = await sm.list_sessions(status=status_filter)
    return web.json_response([s.to_dict() for s in sessions])


async def handle_get_build(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    sm: SessionManager = request.app["session_manager"]
    try:
        session = await sm.get_session(session_id)
    except SessionNotFoundError:
        raise web.HTTPNotFound(text=f"Session '{session_id}' not found.")
    return web.json_response(session.to_dict())


async def handle_stream_build(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events stream for a build session."""
    session_id = request.match_info["session_id"]
    sm: SessionManager = request.app["session_manager"]

    try:
        session = await sm.get_session(session_id)
    except SessionNotFoundError:
        raise web.HTTPNotFound(text=f"Session '{session_id}' not found.")

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)

    # Register subscriber queue
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    subscribers = request.app["sse_subscribers"]
    subscribers.setdefault(session_id, []).append(queue)

    # Send current session snapshot immediately
    await _sse_write(response, "snapshot", session.to_dict())

    # Replay buffered events so late subscribers don't miss fast builds
    buffers = request.app["sse_buffers"]
    for buffered_event_type, buffered_data in list(buffers.get(session_id, [])):
        await _sse_write(response, buffered_event_type, buffered_data)
        if buffered_event_type == "done":
            break

    try:
        while True:
            try:
                event_type, data = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                # Send keepalive comment
                await response.write(b": keepalive\n\n")
                continue

            await _sse_write(response, event_type, data)

            if event_type == "done":
                break
    finally:
        if session_id in subscribers:
            try:
                subscribers[session_id].remove(queue)
            except ValueError:
                pass
        # Clean up buffer when no subscribers remain and build is done
        if not subscribers.get(session_id) and session.status in {
            BuildStatus.COMPLETE,
            BuildStatus.FAILED,
            BuildStatus.CANCELLED,
        }:
            buffers.pop(session_id, None)

    return response


async def _sse_write(
    response: web.StreamResponse, event_type: str, data: Any
) -> None:
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    await response.write(payload.encode())


async def handle_cancel_build(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    sm: SessionManager = request.app["session_manager"]
    try:
        cancelled = await sm.cancel_session(session_id)
    except SessionNotFoundError:
        raise web.HTTPNotFound(text=f"Session '{session_id}' not found.")

    await _broadcast(request.app, session_id, "cancelled", {"session_id": session_id})
    await _broadcast(request.app, session_id, "done", {"session_id": session_id})

    return web.json_response({"cancelled": cancelled, "session_id": session_id})


# ---------------------------------------------------------------------------
# SSE broadcast helper
# ---------------------------------------------------------------------------

_SSE_BUFFER_SIZE = 64


async def _broadcast(
    app: web.Application, session_id: str, event_type: str, data: Any
) -> None:
    # Write to ring buffer so late subscribers can catch up
    buffers = app["sse_buffers"]
    if session_id not in buffers:
        buffers[session_id] = deque(maxlen=_SSE_BUFFER_SIZE)
    buffers[session_id].append((event_type, data))

    subscribers = app["sse_subscribers"].get(session_id, [])
    for q in subscribers:
        try:
            q.put_nowait((event_type, data))
        except asyncio.QueueFull:
            pass  # drop if consumer is too slow


# ---------------------------------------------------------------------------
# Build execution engine
# ---------------------------------------------------------------------------

async def _run_build(app: web.Application, session: BuildSession) -> None:
    sm: SessionManager = app["session_manager"]

    # Check if cancelled before we even start
    fresh = await sm.get_session(session.id)
    if fresh.status == BuildStatus.CANCELLED:
        return

    await sm.mark_running(session.id)
    await _broadcast(app, session.id, "status", {"status": "running", "session_id": session.id})

    # Validate API key at runtime
    if not config.ANTHROPIC_API_KEY:
        await sm.mark_failed(session.id, "ANTHROPIC_API_KEY is not configured.")
        await _broadcast(app, session.id, "error", {"error": "ANTHROPIC_API_KEY is not configured."})
        await _broadcast(app, session.id, "done", {"session_id": session.id})
        return

    executor = ToolExecutor(session_id=session.id)
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": session.task}
    ]

    try:
        async with asyncio.timeout(config.BUILD_TIMEOUT_SECONDS):
            while True:
                # Re-check for cancellation before each API call
                current = await sm.get_session(session.id)
                if current.status == BuildStatus.CANCELLED:
                    return

                response = await client.messages.create(
                    model=session.model,
                    max_tokens=8192,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )

                # Record the assistant message
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content,
                }
                messages.append(assistant_msg)
                await sm.append_message(session.id, {
                    "role": "assistant",
                    "content": [
                        _content_block_to_dict(b) for b in response.content
                    ],
                })
                await _broadcast(app, session.id, "message", {
                    "role": "assistant",
                    "content": [_content_block_to_dict(b) for b in response.content],
                })

                if response.stop_reason == "end_turn":
                    break

                if response.stop_reason == "tool_use":
                    tool_results: list[dict[str, Any]] = []

                    for block in response.content:
                        if block.type != "tool_use":
                            continue

                        tool_name = block.name
                        tool_input = block.input

                        await _broadcast(app, session.id, "tool_call", {
                            "tool": tool_name,
                            "input": tool_input,
                        })

                        result = await executor.execute(tool_name, tool_input)

                        await _broadcast(app, session.id, "tool_result", {
                            "tool": tool_name,
                            "result": result,
                        })

                        # Build tool_result content block
                        is_error = "error" in result
                        result_text = (
                            result.get("error")
                            if is_error
                            else json.dumps(result)
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                            **({"is_error": True} if is_error else {}),
                        })

                    # Add tool results as user turn
                    user_tool_msg: dict[str, Any] = {
                        "role": "user",
                        "content": tool_results,
                    }
                    messages.append(user_tool_msg)
                    await sm.append_message(session.id, user_tool_msg)
                    continue

                # Any other stop reason — exit loop
                break

    except asyncio.TimeoutError:
        await sm.mark_failed(
            session.id,
            f"Build timed out after {config.BUILD_TIMEOUT_SECONDS}s",
        )
        await _broadcast(app, session.id, "error", {"error": "Build timed out."})
        await _broadcast(app, session.id, "done", {"session_id": session.id})
        return
    except anthropic.APIError as exc:
        await sm.mark_failed(session.id, f"Anthropic API error: {exc}")
        await _broadcast(app, session.id, "error", {"error": str(exc)})
        await _broadcast(app, session.id, "done", {"session_id": session.id})
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error in build %s", session.id)
        await sm.mark_failed(session.id, str(exc))
        await _broadcast(app, session.id, "error", {"error": str(exc)})
        await _broadcast(app, session.id, "done", {"session_id": session.id})
        return

    # Check one more time for cancellation before marking complete
    final = await sm.get_session(session.id)
    if final.status == BuildStatus.CANCELLED:
        return

    artifacts = executor.collect_artifacts()
    await sm.mark_complete(session.id, artifacts=artifacts)
    await _broadcast(app, session.id, "complete", {
        "session_id": session.id,
        "artifact_count": len(artifacts),
    })
    await _broadcast(app, session.id, "done", {"session_id": session.id})


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an Anthropic content block to a serialisable dict."""
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if hasattr(block, "__dict__"):
        return {k: v for k, v in block.__dict__.items() if not k.startswith("_")}
    return {"raw": str(block)}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    web.run_app(app, host=config.HOST, port=config.PORT)
