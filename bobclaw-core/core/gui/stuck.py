"""Deterministic stuck detection (PURE predicates + one stateful detector).

Unified §7 *detection* = deterministic bookkeeping, no model: frame-hash repeats,
action dedup, step/time budgets, and a consecutive-veto streak (the §7←§6 wiring, where
the verdict stream is one trip signal). The clock is injected so the detector is fully
deterministic and Docker-verifiable. Model-driven *adjudication* is step-6.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Sequence

from core.gui.types import StuckConfig, StuckSignal


def frame_repeat_count(sigs: Sequence[str]) -> int:
    """Length of the trailing run of the last signature (``[]``→0, ``[a,b,b,b]``→3)."""
    if not sigs:
        return 0
    last = sigs[-1]
    count = 0
    for s in reversed(sigs):
        if s == last:
            count += 1
        else:
            break
    return count


def action_repeat_count(keys: Sequence[str | None], window: int) -> int:
    """Max frequency of any single key in the last ``window`` entries (``None`` ignored)."""
    if window <= 0 or not keys:
        return 0
    tail = keys[-window:] if window < len(keys) else keys
    freq: dict[str, int] = {}
    for k in tail:
        if k is not None:
            freq[k] = freq.get(k, 0) + 1
    return max(freq.values()) if freq else 0


def over_step_budget(n: int, budget: int) -> bool:
    """``n >= budget`` when ``budget > 0`` (``budget <= 0`` means unbounded → False)."""
    return budget > 0 and n >= budget


def over_time_budget(elapsed: float, budget: float) -> bool:
    """``elapsed >= budget`` when ``budget > 0`` (``budget <= 0`` means unbounded → False)."""
    return budget > 0 and elapsed >= budget


def veto_streak(oks: Sequence[bool | None]) -> int:
    """Trailing run of exactly ``False`` (``None``/``True`` stop the run)."""
    count = 0
    for v in reversed(oks):
        if v is False:
            count += 1
        else:
            break
    return count


class StuckDetector:
    """Tracks frame signatures, action keys, and verdicts; trips a :class:`StuckSignal`.

    Fixed precedence: STEP_BUDGET > TIME_BUDGET > NO_PROGRESS > ACTION_REPEAT >
    VETO_STREAK > NONE. ``time_fn`` is injectable (default :func:`time.monotonic`) so
    tests/Docker runs are deterministic.
    """

    def __init__(self, cfg: StuckConfig, *, time_fn: Callable[[], float] | None = None) -> None:
        self.cfg = cfg
        self.time_fn: Callable[[], float] = time_fn if time_fn is not None else time.monotonic
        self._t0: float | None = None
        self.step_count: int = 0
        cap = max(256, cfg.no_change_limit, cfg.action_dedup_window, cfg.veto_streak_limit)
        self._sigs: deque[str] = deque(maxlen=cap)
        self._keys: deque[str | None] = deque(maxlen=cap)
        self._oks: deque[bool | None] = deque(maxlen=cap)

    def start(self) -> None:
        """Stamp the start time (idempotent — only the first call sets it)."""
        if self._t0 is None:
            self._t0 = self.time_fn()

    def record(self, frame_sig: str, action_key: str | None, verdict_ok: bool | None) -> None:
        """Append one step's frame signature, action key, and verdict outcome."""
        self._sigs.append(frame_sig)
        self._keys.append(action_key)
        self._oks.append(verdict_ok)
        self.step_count += 1

    def check(self) -> StuckSignal:
        """Return the first tripped signal in fixed precedence, else ``NONE``."""
        if over_step_budget(self.step_count, self.cfg.max_steps):
            return StuckSignal.STEP_BUDGET
        if self._t0 is not None and over_time_budget(self.time_fn() - self._t0, self.cfg.max_seconds):
            return StuckSignal.TIME_BUDGET
        if frame_repeat_count(self._sigs) >= self.cfg.no_change_limit:
            return StuckSignal.NO_PROGRESS
        if action_repeat_count(list(self._keys), self.cfg.action_dedup_window) >= self.cfg.action_dedup_limit:
            return StuckSignal.ACTION_REPEAT
        if veto_streak(self._oks) >= self.cfg.veto_streak_limit:
            return StuckSignal.VETO_STREAK
        return StuckSignal.NONE
