"""BoBClaw TUI — connection/status-row wiring (MS6-T2 / T1 health seam).

The header health row (T1, landed ``8d1f510``) combines the two PURE sources the app owns
— the reducer's transport-error signal (``MonitorState.health()``) and the ``/ws/monitor``
socket's ``ConnState`` (``live | reconnecting(n) | DOWN:<code>``) — into one honest cell so
a supervisor can tell **idle** from **socket-dead** from **Redis-down**. The composition is
``panels.header_line`` (health · honest EST cost · token tick); this module is the thin app
seam that reads the app's state and produces that string. No I/O, no Textual — so the health
wiring is unit-testable straight from the app object.
"""
from __future__ import annotations

from bobclaw_tui.panels import chat_cell, header_line, pins_cell


def status_row_text(app) -> str:
    """The full cockpit header row for the current app state — health · EST cost · tokens,
    plus the active conversation title (truncated) and its pins (face/model/profile) once
    bound (Wave 1B Tasks 1-2).

    Reads the app's two pure health sources (``_state.health()`` + ``_conn``) and the
    current glyph mode (``_ascii_mode``) and returns the exact string the ``#statusrow``
    Static renders. Kept as a function over the app so the pilot suite can assert the health
    transition (idle→DOWN→live) without a terminal or socket."""
    flights = app._state.flights()
    row = header_line(
        flights,
        app._state.health(),
        app._conn.status,
        app._conn.attempts,
        ascii_mode=app._ascii_mode,
    )
    cells = []
    cell = chat_cell(getattr(app._chat, "conversation_title", None))
    if cell:
        cells.append(f"chat: {cell}")
    # getattr: fake chat clients in the pilot suite predate the pin surface (Task 2)
    pins = pins_cell(
        getattr(app._chat, "face_pin", None),
        getattr(app._chat, "model_pin", None),
        getattr(app._chat, "backend_pin", None),
        getattr(app._chat, "profile_pin", None),
    )
    if pins:
        cells.append(pins)
    # busy-turn queue (Wave 1B Task 5): queued count visible while a turn streams
    queued = len(getattr(app, "_queue", ()) or ())
    if queued:
        cells.append(f"queued: {queued}")
    return f"{row}   {'   '.join(cells)}" if cells else row
