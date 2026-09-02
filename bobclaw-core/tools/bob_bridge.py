#!/usr/bin/env python3
"""Loopback-only OpenAI-compatible front door for BoB's chat engine."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

# Running this file directly puts bobclaw-core/tools on sys.path.  Add the core
# root so the existing face registry is available without packaging changes.
CORE_ROOT = Path(__file__).resolve().parents[1]
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

from core.faces.registry import FaceRegistry  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8902
DEFAULT_ENGINE = "http://127.0.0.1:7835"
DEFAULT_MAX_HISTORY = 30
_SESSION_NAMESPACE = uuid.UUID("75763c0f-ed7d-4585-a314-7470e22b464d")
_HOP_HEADERS = {"connection", "content-length", "host", "transfer-encoding"}

FACES_KEY = web.AppKey("bob_bridge_faces", FaceRegistry)
ENGINE_KEY = web.AppKey("bob_bridge_engine", str)
UPSTREAM_KEY = web.AppKey("bob_bridge_upstream", object)
MAX_HISTORY_KEY = web.AppKey("bob_bridge_max_history", int)
TURNS_KEY = web.AppKey("bob_bridge_turns", Counter)


class BridgeError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def _error(message: str, *, status: int, code: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "bridge_error", "code": code}},
        status=status,
    )


def _safe_field(value: Any) -> str:
    return str(value or "-").replace("\n", "\\n").replace("\r", "\\r")[:100]


def _log(*, model: str, face: str, chunks: int, started: float, status: int,
         session: str = "-", turn: int = 0, lane: str = "bob") -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    elapsed = int((time.monotonic() - started) * 1000)
    print(
        f"{stamp} lane={_safe_field(lane)} model={_safe_field(model)} "
        f"face={_safe_field(face)} session={_safe_field(session)} turn={turn} "
        f"chunks={chunks} ms={elapsed} status={status}",
        flush=True,
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                raise BridgeError("message content parts must be objects")
            if part.get("type") in {"text", "input_text", "output_text"}:
                text = part.get("text")
                if not isinstance(text, str):
                    raise BridgeError("text content must be a string")
                parts.append(text)
            elif part.get("type") not in {"input_image", "image_url"}:
                raise BridgeError(f"unsupported message content type: {part.get('type')!r}")
            else:
                raise BridgeError("bob bridge currently accepts text content only")
        return "\n".join(parts)
    if content is None:
        return ""
    raise BridgeError("message content must be a string or text-part list")


def _chat_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise BridgeError("messages must be a non-empty list")
    messages: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise BridgeError("each message requires a string role and content")
        messages.append({"role": item["role"].lower(), "content": _content_text(item.get("content"))})
    return messages


def _responses_messages(body: dict[str, Any]) -> tuple[list[dict[str, str]], int]:
    messages: list[dict[str, str]] = []
    instructions = body.get("instructions")
    if instructions:
        if not isinstance(instructions, str):
            raise BridgeError("instructions must be a string")
        messages.append({"role": "system", "content": instructions})

    raw = body.get("input")
    if isinstance(raw, str):
        return messages + [{"role": "user", "content": raw}], 1
    if not isinstance(raw, list) or not raw:
        raise BridgeError("input must be a non-empty string or list")
    for item in raw:
        if not isinstance(item, dict):
            raise BridgeError("response input items must be objects")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role", "user")
            if not isinstance(role, str):
                raise BridgeError("response message role must be a string")
            messages.append({"role": role.lower(), "content": _content_text(item.get("content"))})
        elif kind == "function_call_output":
            messages.append({"role": "tool", "content": _content_text(item.get("output"))})
        elif kind == "function_call":
            call = f"{item.get('name', 'function')}({item.get('arguments', '')})"
            messages.append({"role": "assistant", "content": call})
        elif kind not in {"reasoning", "item_reference"}:
            raise BridgeError(f"unsupported response input type: {kind!r}")
    return messages, len(raw)


def flatten_messages(messages: list[dict[str, str]]) -> str:
    system: list[str] = []
    transcript: list[str] = []
    labels = {"user": "User", "assistant": "Assistant", "tool": "Tool"}
    for message in messages:
        role, content = message["role"], message["content"].strip()
        if not content:
            continue
        if role in {"system", "developer"}:
            system.append(content)
        else:
            transcript.append(f"{labels.get(role, role.title())}: {content}")
    sections: list[str] = []
    if system:
        sections.append("System:\n" + "\n\n".join(system))
    if transcript:
        sections.append("Conversation:\n" + "\n".join(transcript))
    prompt = "\n\n".join(sections).strip()
    if not prompt:
        raise BridgeError("request contains no text content")
    return prompt


def _identity(request: web.Request, body: dict[str, Any], messages: list[dict[str, str]]) -> tuple[str, str]:
    explicit = request.headers.get("session-id") or request.headers.get("x-session-id")
    for key in ("session_id", "conversation_id", "prompt_cache_key"):
        value = body.get(key)
        if not explicit and isinstance(value, str) and value.strip():
            explicit = value.strip()
    for key in ("metadata", "client_metadata"):
        metadata = body.get(key)
        if not explicit and isinstance(metadata, dict):
            for field in ("session_id", "conversation_id", "thread_id"):
                value = metadata.get(field)
                if isinstance(value, str) and value.strip():
                    explicit = value.strip()
                    break

    if explicit:
        seed = f"explicit:{explicit}"
        try:
            conversation_id = str(uuid.UUID(explicit))
        except ValueError:
            conversation_id = str(uuid.uuid5(_SESSION_NAMESPACE, seed))
    else:
        first_system = next((m["content"] for m in messages if m["role"] in {"system", "developer"}), "")
        first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
        seed = f"fallback:{first_system}\0{first_user}"
        conversation_id = str(uuid.uuid5(_SESSION_NAMESPACE, seed))
    return conversation_id, hashlib.sha256(seed.encode()).hexdigest()[:12]


def _face_for_model(model: str, faces: FaceRegistry) -> str:
    normalized = model.lower()
    if normalized == "bob":
        face_id = "assistant"
    elif normalized.startswith("bob-") and len(normalized) > 4:
        face_id = normalized[4:]
    else:
        face_id = normalized.removeprefix("bob") or "assistant"
    try:
        face = faces.get_face(face_id)
    except KeyError as exc:
        raise BridgeError(
            f"Unknown bob face {face_id!r}; choose a model returned by /v1/models",
            status=503,
            code="face_unavailable",
        ) from exc
    if face.role != "worker":
        raise BridgeError(
            f"Face {face_id!r} has role {face.role!r}; bob bridge allows worker faces only "
            "to prevent planner/backend recursion",
            status=503,
            code="face_role_blocked",
        )
    return face_id


def _usage(event: dict[str, Any]) -> dict[str, int]:
    nested = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    prompt = int(event.get("tokens_in", nested.get("prompt_tokens", nested.get("input_tokens", 0))) or 0)
    completion = int(event.get("tokens_out", nested.get("completion_tokens", nested.get("output_tokens", 0))) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


async def _consume_bob(
    response: aiohttp.ClientResponse,
    on_chunk: Callable[[str], Awaitable[None]],
) -> tuple[str, dict[str, int], int]:
    pieces: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    complete = False
    chunk_count = 0
    while True:
        raw = await response.content.readline()
        if not raw:
            break
        if not raw.startswith(b"data:"):
            continue
        try:
            event = json.loads(raw[5:].strip())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BridgeError("bob engine returned malformed SSE", status=502, code="engine_protocol") from exc
        kind = event.get("type")
        if kind == "chunk":
            delta = event.get("content") or ""
            if delta:
                pieces.append(delta)
                chunk_count += 1
                await on_chunk(delta)
        elif kind == "message_complete":
            usage = _usage(event)
            complete = True
        elif kind == "error":
            raise BridgeError(
                f"bob engine error: {event.get('message', 'unknown error')}",
                status=502,
                code="engine_error",
            )
        elif kind == "approval_request":
            raise BridgeError(
                "bob engine requested an approval that this completion protocol cannot carry",
                status=502,
                code="approval_unsupported",
            )
    if not complete:
        raise BridgeError("bob engine stream ended without message_complete", status=502, code="engine_protocol")
    return "".join(pieces), usage, chunk_count


def _chat_chunk(completion_id: str, model: str, *, delta: dict[str, str], finish: str | None = None,
                usage: dict[str, int] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        item["usage"] = usage
    return item


async def _sse_write(response: web.StreamResponse, event: dict[str, Any], *, name: str | None = None) -> None:
    prefix = f"event: {name}\n" if name else ""
    await response.write(f"{prefix}data: {json.dumps(event, separators=(',', ':'))}\n\n".encode())


async def _bob_completion(
    request: web.Request,
    body: dict[str, Any],
    messages: list[dict[str, str]],
    message_count: int,
    *,
    wire: str,
) -> web.StreamResponse:
    started = time.monotonic()
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise BridgeError("model must be a non-empty string")
    face = _face_for_model(model, request.app[FACES_KEY])
    maximum = request.app[MAX_HISTORY_KEY]
    if message_count > maximum:
        raise BridgeError(
            f"request has {message_count} messages; BOB_BRIDGE_MAX_HISTORY is {maximum}. "
            "Start a fresh CLI session to avoid transcript-replay storms.",
            status=400,
            code="history_limit",
        )

    content = flatten_messages(messages)
    conversation_id, session_log = _identity(request, body, messages)
    turns = request.app[TURNS_KEY]
    turns[conversation_id] += 1
    turn = turns[conversation_id]
    engine_payload = {
        "conversation_id": conversation_id,
        "content": content,
        "face_id": face,
        "pin_authoritative": True,
    }
    stream = body.get("stream") is True
    completion_id = ("chatcmpl-" if wire == "chat" else "resp_") + uuid.uuid4().hex
    item_id = "msg_" + uuid.uuid4().hex
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600)
    chunks = 0

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(request.app[ENGINE_KEY] + "/api/chat", json=engine_payload) as upstream:
                if upstream.status != 200:
                    detail = (await upstream.text())[:1000]
                    raise BridgeError(
                        f"bob engine returned HTTP {upstream.status}: {detail}",
                        status=502,
                        code="engine_http_error",
                    )

                if not stream:
                    async def discard(_: str) -> None:
                        return None

                    text, usage, chunks = await _consume_bob(upstream, discard)
                    if wire == "chat":
                        payload = {
                            "id": completion_id,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                         "finish_reason": "stop"}],
                            "usage": usage,
                        }
                    else:
                        payload = _response_object(completion_id, item_id, model, text, usage)
                    _log(model=model, face=face, chunks=chunks, started=started, status=200,
                         session=session_log, turn=turn)
                    return web.json_response(payload)

                downstream = web.StreamResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"},
                )
                await downstream.prepare(request)
                if wire == "chat":
                    await _sse_write(downstream, _chat_chunk(
                        completion_id, model, delta={"role": "assistant", "content": ""}
                    ))
                else:
                    created = {"type": "response.created", "response": {"id": completion_id}}
                    await _sse_write(downstream, created, name=created["type"])

                async def forward(delta: str) -> None:
                    if wire == "chat":
                        await _sse_write(downstream, _chat_chunk(
                            completion_id, model, delta={"content": delta}
                        ))
                    else:
                        event = {"type": "response.output_text.delta", "delta": delta,
                                 "item_id": item_id, "output_index": 0, "content_index": 0}
                        await _sse_write(downstream, event, name=event["type"])

                try:
                    text, usage, chunks = await _consume_bob(upstream, forward)
                    if wire == "chat":
                        await _sse_write(downstream, _chat_chunk(
                            completion_id, model, delta={}, finish="stop", usage=usage
                        ))
                        await downstream.write(b"data: [DONE]\n\n")
                    else:
                        done = {"type": "response.output_item.done", "output_index": 0,
                                "item": _response_message(item_id, text)}
                        await _sse_write(downstream, done, name=done["type"])
                        completed = {"type": "response.completed",
                                     "response": _response_object(completion_id, item_id, model, text, usage)}
                        await _sse_write(downstream, completed, name=completed["type"])
                    await downstream.write_eof()
                    _log(model=model, face=face, chunks=chunks, started=started, status=200,
                         session=session_log, turn=turn)
                    return downstream
                except ConnectionResetError:
                    _log(model=model, face=face, chunks=chunks, started=started, status=499,
                         session=session_log, turn=turn)
                    return downstream
                except (BridgeError, aiohttp.ClientError, asyncio.TimeoutError) as failure:
                    exc = failure if isinstance(failure, BridgeError) else BridgeError(
                        f"bob engine stream failed: {failure}", status=502, code="engine_unavailable"
                    )
                    if wire == "chat":
                        await _sse_write(downstream, {"error": {"message": str(exc), "type": "bridge_error",
                                                                "code": exc.code}})
                        await downstream.write(b"data: [DONE]\n\n")
                    else:
                        failed = {"type": "response.failed", "response": {"id": completion_id,
                                  "error": {"message": str(exc), "type": "bridge_error", "code": exc.code}}}
                        await _sse_write(downstream, failed, name=failed["type"])
                    await downstream.write_eof()
                    _log(model=model, face=face, chunks=chunks, started=started, status=exc.status,
                         session=session_log, turn=turn)
                    return downstream
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise BridgeError(f"cannot reach bob engine: {exc}", status=502, code="engine_unavailable") from exc


def _response_message(item_id: str, text: str) -> dict[str, Any]:
    return {"id": item_id, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _response_object(response_id: str, item_id: str, model: str, text: str,
                     usage: dict[str, int]) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [_response_message(item_id, text)],
        "output_text": text,
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "input_tokens_details": None,
            "output_tokens": usage["completion_tokens"],
            "output_tokens_details": None,
            "total_tokens": usage["total_tokens"],
        },
    }


def _target(upstream: str, endpoint: str) -> str:
    base = upstream.rstrip("/")
    return base if base.endswith("/" + endpoint) else f"{base}/{endpoint}"


async def _passthrough(request: web.Request, body: dict[str, Any], endpoint: str) -> web.StreamResponse:
    started = time.monotonic()
    model = body.get("model") if isinstance(body.get("model"), str) else "-"
    upstream = request.app[UPSTREAM_KEY]
    if not upstream:
        _log(model=model, face="-", chunks=0, started=started, status=501, lane="passthrough")
        return _error(
            "Non-bob models require BOB_BRIDGE_UPSTREAM to be set to an OpenAI-compatible base URL",
            status=501,
            code="upstream_not_configured",
        )
    headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS}
    chunks = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600)) as session:
            async with session.post(_target(str(upstream), endpoint), json=body, headers=headers) as response:
                if body.get("stream") is True:
                    downstream = web.StreamResponse(status=response.status,
                                                    headers={"Content-Type": response.headers.get(
                                                        "Content-Type", "text/event-stream")})
                    await downstream.prepare(request)
                    async for chunk in response.content.iter_any():
                        chunks += 1
                        await downstream.write(chunk)
                    await downstream.write_eof()
                    _log(model=model, face="-", chunks=chunks, started=started,
                         status=response.status, lane="passthrough")
                    return downstream
                data = await response.read()
                _log(model=model, face="-", chunks=0, started=started,
                     status=response.status, lane="passthrough")
                return web.Response(body=data, status=response.status,
                                    content_type=response.content_type or "application/json")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        _log(model=model, face="-", chunks=chunks, started=started, status=502, lane="passthrough")
        return _error(f"cannot reach BOB_BRIDGE_UPSTREAM: {exc}", status=502, code="upstream_unavailable")


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise BridgeError("request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise BridgeError("request body must be a JSON object")
    return body


async def chat_completions(request: web.Request) -> web.StreamResponse:
    started = time.monotonic()
    model = "-"
    try:
        body = await _json_body(request)
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise BridgeError("model must be a non-empty string")
        if not model.lower().startswith("bob"):
            return await _passthrough(request, body, "chat/completions")
        messages = _chat_messages(body)
        return await _bob_completion(request, body, messages, len(messages), wire="chat")
    except BridgeError as exc:
        _log(model=model if isinstance(model, str) else "-", face="-", chunks=0,
             started=started, status=exc.status)
        return _error(str(exc), status=exc.status, code=exc.code)


async def responses(request: web.Request) -> web.StreamResponse:
    """Minimal Responses API lane required by current Codex releases."""
    started = time.monotonic()
    model = "-"
    try:
        body = await _json_body(request)
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise BridgeError("model must be a non-empty string")
        if not model.lower().startswith("bob"):
            return await _passthrough(request, body, "responses")
        messages, count = _responses_messages(body)
        return await _bob_completion(request, body, messages, count, wire="responses")
    except BridgeError as exc:
        _log(model=model if isinstance(model, str) else "-", face="-", chunks=0,
             started=started, status=exc.status)
        return _error(str(exc), status=exc.status, code=exc.code)


async def models(request: web.Request) -> web.Response:
    now = int(time.time())
    face_models = [f"bob-{face.id}" for face in request.app[FACES_KEY].all_faces()
                   if face.role == "worker"]
    ids = ["bob", *face_models]
    return web.json_response({"object": "list", "data": [
        {"id": model_id, "object": "model", "created": now, "owned_by": "bob-bridge"}
        for model_id in ids
    ]})


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "bob-bridge"})


def build_app(*, engine_url: str | None = None, upstream: str | None = None,
              max_history: int | None = None, faces: FaceRegistry | None = None) -> web.Application:
    engine = (engine_url or os.environ.get("BOB_BRIDGE_ENGINE", DEFAULT_ENGINE)).rstrip("/")
    configured_upstream = upstream if upstream is not None else os.environ.get("BOB_BRIDGE_UPSTREAM")
    maximum = max_history if max_history is not None else int(
        os.environ.get("BOB_BRIDGE_MAX_HISTORY", str(DEFAULT_MAX_HISTORY))
    )
    if maximum < 1:
        raise ValueError("BOB_BRIDGE_MAX_HISTORY must be at least 1")
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app[FACES_KEY] = faces or FaceRegistry()
    app[ENGINE_KEY] = engine
    app[UPSTREAM_KEY] = configured_upstream
    app[MAX_HISTORY_KEY] = maximum
    app[TURNS_KEY] = Counter()
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/v1/responses", responses)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("BOB_BRIDGE_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    web.run_app(build_app(), host=HOST, port=args.port)


if __name__ == "__main__":
    main()
