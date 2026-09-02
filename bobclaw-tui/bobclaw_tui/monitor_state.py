"""BoBClaw TUI — monitor-state reducer (Lane 1c, the tested data layer).

Consumes the flight substrate's monitor frames (``/ws/monitor``; shapes per
GATEWAY-CONTRACT) and maintains a per-flight aggregate the panes render — grouped by
``flight_id`` (Q3 two-tier: named "block of work" flights AND ambient ``chat:*`` / ``ambient``
work all get a lane, so nothing is invisible). PURE: no I/O, no clock — ``apply`` is a plain
reducer, so the whole view is deterministic and unit-testable without a terminal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Worker statuses considered terminal (vs the mid-run "running").
_TERMINAL = {"ok", "failed", "timeout", "rejected", "flagged", "no_impl", "unsafe"}


@dataclass
class FlightView:
    """Everything known about ONE flight, aggregated from its frames."""
    flight_id: str
    # per-worker latest state: idx -> {status, backend, role, name, duration_ms}
    workers: dict = field(default_factory=dict)
    # latest fan-out wave marker: {n_workers, wave, backend, kind}
    fleet: dict = field(default_factory=dict)
    # latest join reduce: {ok, failed, total, kind}
    join: dict = field(default_factory=dict)
    # council: {seats: {idx -> {...}}, synth: {...}}
    council: dict = field(default_factory=lambda: {"seats": {}, "synth": {}})
    # spend: {usd, by_backend}
    cost: dict = field(default_factory=lambda: {"usd": 0.0, "by_backend": {}})
    # last frame ts seen for this flight (ISO string, or None)
    last_ts: Optional[str] = None
    # total frames applied (activity indicator)
    events: int = 0

    # ── derived counters (what a fleet pane shows at a glance) ──
    def running(self) -> int:
        return sum(1 for w in self.workers.values() if w.get("status") == "running")

    def done(self) -> int:
        return sum(1 for w in self.workers.values() if w.get("status") in _TERMINAL)

    def failed(self) -> int:
        return sum(
            1 for w in self.workers.values()
            if w.get("status") in ("failed", "timeout", "rejected")
        )

    def is_ambient(self) -> bool:
        fid = self.flight_id or ""
        return fid == "ambient" or fid.startswith("chat:")

    def is_quiescent(self) -> bool:
        """No worker/seat is mid-run — the flight is done (or idle). The app's reaper
        only evicts quiescent flights, so an in-flight one is never reaped."""
        if self.running() > 0:
            return False
        return not any(s.get("status") == "running" for s in self.council["seats"].values())

    def tokens(self) -> int:
        """Estimated tokens across this flight's workers + council seats (the honest
        per-flight resource tick — no per-backend USD table exists). Latest-per-idx,
        so a worker's terminal frame supersedes its ``running`` (token-less) frame."""
        wt = sum(int(w.get("tokens") or 0) for w in self.workers.values())
        st = sum(int(s.get("tokens") or 0) for s in self.council["seats"].values())
        return wt + st


class MonitorState:
    """The whole cockpit's model: flight_id -> FlightView. Feed frames via :meth:`apply`."""

    def __init__(self) -> None:
        self._flights: dict[str, FlightView] = {}
        # Connection-level (transport) health: the latest `error` frame the monitor
        # socket forwarded ({code, message, ts}), or None when the stream is healthy.
        # NOT per-flight — redis_unavailable / forbidden affect the whole /ws/monitor
        # stream, so a Redis-down signal must not hide inside one flight's lane (T2).
        self._health: Optional[dict] = None

    def _view(self, flight_id: Optional[str]) -> FlightView:
        fid = flight_id or "ambient"
        if fid not in self._flights:
            self._flights[fid] = FlightView(flight_id=fid)
        return self._flights[fid]

    def apply(self, frame: dict) -> None:
        """Fold one monitor frame into the per-flight state. Unknown/malformed frames are
        ignored (a forward-compatible viewer never crashes on a new frame type)."""
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")

        # ── connection-level (transport) frame — NOT per-flight (T2) ──
        # An `error` frame (redis_unavailable | forbidden | decode_error |
        # unsupported_message | invalid_message) is a health/transport signal from the
        # monitor socket, not work on a flight. Record it as connection health and do
        # NOT create/touch a FlightView (else a phantom `ambient` lane appears and its
        # events tick on every Redis-down heartbeat). It stays PURE — no I/O, no clock.
        if kind == "error":
            self._health = {
                "code": frame.get("code"),
                "message": frame.get("message"),
                "ts": frame.get("ts"),
            }
            return
        # Any parseable non-error frame proves the stream is delivering again → the
        # transport-error health clears itself (recovery is OBSERVED from a real good
        # frame, never guessed). This is the "cleared on next healthy frame" rule.
        self._health = None

        view = self._view(frame.get("flight_id"))
        view.events += 1
        if frame.get("ts"):
            view.last_ts = frame["ts"]

        if kind == "fleet_start":
            view.fleet = {k: frame.get(k) for k in ("n_workers", "wave", "backend", "kind")}
        elif kind == "worker_state":
            idx = frame.get("idx")
            if idx is not None:
                view.workers[idx] = {
                    "status": frame.get("status"),
                    "backend": frame.get("backend"),
                    "role": frame.get("role"),
                    "name": frame.get("name"),
                    "duration_ms": frame.get("duration_ms"),
                    "tokens": frame.get("tokens"),
                }
        elif kind == "fleet_join":
            view.join = {k: frame.get(k) for k in ("ok", "failed", "total", "kind")}
        elif kind == "council_seat":
            idx = frame.get("idx")
            if idx is not None:
                view.council["seats"][idx] = {
                    "posture": frame.get("posture"), "backend": frame.get("backend"),
                    "status": frame.get("status"), "round": frame.get("round"),
                    "tokens": frame.get("tokens"),
                }
        elif kind == "council_synth":
            view.council["synth"] = {
                "backend": frame.get("backend"), "status": frame.get("status"),
                "shape": frame.get("shape"),
            }
        elif kind == "cost":
            view.cost = {
                "usd": frame.get("usd", view.cost.get("usd", 0.0)),
                "by_backend": frame.get("by_backend", view.cost.get("by_backend", {})),
            }
        # else: unknown type — counted (events++) but otherwise ignored.

    def evict(self, flight_id: str) -> bool:
        """Drop a flight from the view (the app's reaper / prune calls this once a flight
        is quiescent + idle — the clock lives in the app, so ``apply`` stays pure)."""
        return self._flights.pop(flight_id, None) is not None

    def health(self) -> Optional[dict]:
        """Latest connection-level transport error ({code, message, ts}), or None when
        the /ws/monitor stream is healthy. Set by an `error` frame (redis_unavailable,
        forbidden, ...) and cleared by the next healthy frame. This is a *connection*
        signal, not per-flight: it lets the header tell socket-live-but-Redis-down from
        an idle-but-healthy stream. The clock/reconnect state lives in ``monitor_client``;
        this reducer stays pure."""
        return self._health

    def flight(self, flight_id: str) -> Optional[FlightView]:
        return self._flights.get(flight_id)

    def flights(self) -> list[FlightView]:
        """All flights, NAMED (blocks of work) first then ambient, each alpha by id — a
        stable order for the pane (named work is what a supervisor watches first)."""
        return sorted(
            self._flights.values(), key=lambda v: (v.is_ambient(), v.flight_id),
        )

    def total_running(self) -> int:
        return sum(v.running() for v in self._flights.values())
