from __future__ import annotations

"""The hands seam (unified §2.2) + a deterministic in-memory FakeSurface.

``Surface`` is the uniform abstract interface real adapters (pyautogui desktop /
CDP browser / ADB android) will implement — those are the v1-next seam, deliberately
OUT of step-1 (they need a display and aren't Docker-verifiable). ``FakeSurface`` is a
scripted, no-I/O state machine so the whole loop runs headless and deterministically.
"""

from abc import ABC, abstractmethod

from core.gui.actions import format_action
from core.gui.types import Action, ActionResult, Frame

__all__ = ("Surface", "FakeSurface")


class Surface(ABC):
    """Abstract GUI surface adapter: capture a frame, actuate an action.

    Concrete adapters provide real screen I/O and are deferred. ``FakeSurface`` is the
    deterministic step-1 stand-in.
    """

    @abstractmethod
    def capture(self) -> Frame:
        """Return a fresh frame capturing the current surface state."""
        ...

    @abstractmethod
    def act(self, action: Action) -> ActionResult:
        """Actuate *action*; ``performed`` reports actuation, NOT semantic success."""
        ...

    def reset(self) -> None:
        """Reset the surface to its initial state (default no-op)."""


class FakeSurface(Surface):
    """In-memory, deterministic scripted surface state machine (no I/O).

    Built from ``states`` (state-name → :class:`Frame`), a ``transitions`` table
    (``{(state, format_action(action)): next_state}``) and a ``start`` state. A
    transition HIT advances state; a MISS leaves state unchanged but still reports
    ``performed=True`` — the *silent-failure* model the loop must catch via frame-diff,
    not via the surface. Actions in ``inject_error`` report ``performed=False``.
    """

    def __init__(
        self,
        states: dict[str, Frame],
        transitions: dict[tuple[str, str], str],
        start: str,
        *,
        inject_error: set[str] | None = None,
    ) -> None:
        if start not in states:
            raise ValueError(f"start state {start!r} not found in states")
        # copy so a caller mutating/reusing the passed dicts can't leak across instances
        self._states = dict(states)
        self._transitions = dict(transitions)
        self._start = start
        self._cur = start
        self._seq = 0
        self._inject_error = set(inject_error) if inject_error else set()

    def capture(self) -> Frame:
        """Return a new frame for the current state with an incrementing ``seq``.

        ``seq`` advances on every capture but ``image_hash``/``a11y`` stay identical for
        an unchanged state — so the stuck detector still sees "no progress" via
        ``frame_signature`` (which ignores ``seq``).
        """
        self._seq += 1
        base = self._states[self._cur]
        return Frame(seq=self._seq, size=base.size, image_hash=base.image_hash, a11y=base.a11y)

    def act(self, action: Action) -> ActionResult:
        """Look up the transition for *action*; advance on a hit, silent-no-op on a miss."""
        key = format_action(action)
        if key in self._inject_error:
            return ActionResult(performed=False, error=f"injected:{key}")
        nxt = self._transitions.get((self._cur, key))
        if nxt is not None:
            self._cur = nxt
        return ActionResult(performed=True)

    def reset(self) -> None:
        """Reset to the start state and zero the sequence counter."""
        self._cur = self._start
        self._seq = 0
