"""BoBClaw Gateway — Flight substrate live monitor (Layer 0, L0.3).

``GET /ws/monitor`` — a read-only WebSocket that forwards the flight substrate's live
telemetry (fleet fan-out + council seats + per-flight cost) to a watcher (the Bob TUI).

Mirrors ``routers/approvals.py``'s ``/ws/approvals`` — the PROVEN Redis-pub/sub read-only
WS template — which de-risks the multi-process fan-in the intake flagged: core workers
(a separate process) publish monitor frames to Redis channel ``bobclaw:monitor``
(``core.telemetry.emit``); this router subscribes and relays.

Auth mirrors ``/ws/approvals`` (JWT via header or first ``{"type":"auth"}`` frame;
``allow_agent=False`` — a scoped agent token can never watch the fleet). Frames are the
core emit frames verbatim (snake_case, top-level ``type``, FLAT). An optional
``?flight_id=<id>`` query narrows the stream to ONE flight (the TUI's per-flight pane);
absent ⇒ every flight (the TUI groups by ``flight_id`` client-side).
"""
import asyncio
import json
import logging

from aiohttp import WSMsgType, web
from redis.exceptions import TimeoutError as RedisTimeoutError

from auth import authenticate_ws
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = web.RouteTableDef()

# The single monitor channel core publishes to (see core/telemetry/emit.MONITOR_CHANNEL).
MONITOR_CHANNEL = "bobclaw:monitor"


@router.get("/ws/monitor")
async def monitor_socket(request: web.Request) -> web.StreamResponse:
    """Subscribe to the live flight monitor stream.

    Auth pattern mirrors /ws/approvals: HTTP Authorization header OR first JSON
    message ``{"type": "auth", "token": "..."}``. Read-only: the server never acts on
    client frames (a client may ping / close). ``?flight_id=`` optionally filters.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    payload, _ = await authenticate_ws(request, ws, allow_agent=False)
    if payload is None:
        return ws

    # Optional per-flight filter (the TUI's single-flight pane). Empty ⇒ all flights.
    flight_filter = request.query.get("flight_id") or None

    pubsub = None
    try:
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(MONITOR_CHANNEL)
    except Exception as exc:
        await ws.send_json(
            {"type": "error", "message": f"redis unavailable: {exc}", "code": "redis_unavailable"}
        )
        await ws.close()
        return ws

    async def _forward_redis_to_ws() -> None:
        # redis-py 8.x pubsub reads carry a ~5s socket read-timeout, so the blocking
        # ``async for message in pubsub.listen()`` RAISES ``TimeoutError`` on the first
        # idle gap and kills the forward loop — frames published later (e.g. between
        # fan-out waves) then never reach the watcher. Poll with ``get_message`` instead:
        # it returns ``None`` on an idle tick (and we swallow a raised read-timeout
        # defensively) so the loop survives the gaps. Regression-caught by the FU3 live
        # /ws/monitor confirmation; the deterministic gate uses the in-turn stream fork
        # and never exercised this Redis read path.
        try:
            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                except (asyncio.TimeoutError, RedisTimeoutError):
                    continue
                if message is None or message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode(errors="ignore")
                try:
                    parsed = json.loads(data) if isinstance(data, str) else data
                except (ValueError, json.JSONDecodeError):
                    parsed = {"type": "raw", "data": data}
                # Per-flight filter: drop frames for other flights when a filter is set.
                if (
                    flight_filter is not None
                    and isinstance(parsed, dict)
                    and parsed.get("flight_id") != flight_filter
                ):
                    continue
                await ws.send_json(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("monitor WS forward failed: %s", exc)

    forward_task = asyncio.create_task(_forward_redis_to_ws())
    try:
        async for incoming in ws:
            # Read-only: we don't expect commands. Just watch for close/error.
            if incoming.type in {
                WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR,
            }:
                break
    finally:
        forward_task.cancel()
        try:
            await forward_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await pubsub.unsubscribe(MONITOR_CHANNEL)
            await pubsub.aclose()
        except Exception:
            pass
    return ws
