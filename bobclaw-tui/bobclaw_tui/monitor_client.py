"""BoBClaw TUI — /ws/monitor WS client (Lane 1c).

Thin async client: connect to the gateway ``/ws/monitor`` (auth via Bearer header),
parse each JSON frame, and hand it to a callback (the TUI feeds it into a
``MonitorState``). Reconnects with backoff so a gateway blip doesn't kill the cockpit.
Kept dependency-light (aiohttp) and callback-driven so the reducer stays testable.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

FrameCB = Callable[[dict], None]


@dataclass
class ConnState:
    """The /ws/monitor socket's connection health — the ``live | reconnecting(n)`` half of
    the header health row (the other half is the reducer's transport-error ``health()``).

    PURE data: ``stream_monitor`` updates it on connect/disconnect, the app *reads* it in
    its render loop, and ``panels.health_line`` renders it. No I/O, no clock here — so the
    header signal is unit-testable and the app only wires the two pure sources together.
    """
    status: str = "connecting"   # "connecting" (pre-first-connect) | "live" | "reconnecting"
    attempts: int = 0            # consecutive reconnect attempts since the last good connect

    def note_connected(self) -> None:
        """A good ws_connect landed → live; the backoff attempt count resets."""
        self.status = "live"
        self.attempts = 0

    def note_disconnected(self) -> None:
        """The socket dropped (error or clean close) and we're about to back off → count it."""
        self.status = "reconnecting"
        self.attempts += 1


async def stream_monitor(
    url: str,
    token: str,
    on_frame: FrameCB,
    *,
    flight_id: Optional[str] = None,
    stop: Optional[asyncio.Event] = None,
    reconnect_max_s: float = 10.0,
    conn: Optional[ConnState] = None,
) -> None:
    """Stream ``/ws/monitor`` frames into ``on_frame`` until ``stop`` is set.

    Reconnects with exponential backoff (capped at ``reconnect_max_s``) on any connection
    error, so the cockpit survives a gateway restart. ``flight_id`` narrows to one flight.
    An optional ``conn`` (:class:`ConnState`) is updated on connect/disconnect so the
    header health row can show ``live | reconnecting(n)`` — the clean state seam the pure
    reducer deliberately does NOT own (it has no clock/socket).
    """
    import aiohttp

    q = f"?flight_id={flight_id}" if flight_id else ""
    full = f"{url}{q}"
    headers = {"Authorization": f"Bearer {token}"}
    backoff = 0.5
    while stop is None or not stop.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(full, headers=headers, heartbeat=30) as ws:
                    backoff = 0.5  # reset on a good connect
                    if conn is not None:
                        conn.note_connected()
                    async for msg in ws:
                        if stop is not None and stop.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                            aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        try:
                            on_frame(json.loads(msg.data))
                        except (ValueError, json.JSONDecodeError):
                            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any failure
            logger.warning("monitor connection failed (%s); retrying in %.1fs", exc, backoff)
        if stop is not None and stop.is_set():
            break
        # About to back off + reconnect (whether we hit an error or the server closed the
        # socket cleanly) → mark the transport as reconnecting for the header row.
        if conn is not None:
            conn.note_disconnected()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, reconnect_max_s)
