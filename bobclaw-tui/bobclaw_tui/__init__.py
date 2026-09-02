"""BoBClaw TUI (Lane 1c) — a Textual cockpit that WATCHES the fleet/council.

The View plane of the flight substrate: it renders the data plane (``/ws/monitor``),
grouped per flight. Split so the substance is testable without a terminal:

  * ``monitor_state`` — PURE reducers: monitor frames → a per-flight fleet/council/cost
    model (the panes render this; fully unit-tested, no Textual).
  * ``monitor_client`` — a thin aiohttp ``/ws/monitor`` WS client that feeds frames in.
  * ``app`` — the Textual app (import-guarded; Textual optional). Untestable in CI (no TTY),
    so it stays a thin renderer over the tested state.

Run: ``scripts/win/start-tui.ps1`` (installs textual if missing) or ``python -m bobclaw_tui``.
"""
from bobclaw_tui.monitor_state import FlightView, MonitorState

__all__ = ["MonitorState", "FlightView"]
