"""Shared builders for the GUI step-1 deterministic test suite."""
from __future__ import annotations

from core.gui import A11yNode, Frame


def node(role: str, name: str = "", value: str = "", node_id: str = "") -> A11yNode:
    return A11yNode(role=role, name=name, value=value, node_id=node_id)


def frame(image_hash: str, *nodes: A11yNode, seq: int = 0, size: tuple[int, int] = (100, 100)) -> Frame:
    return Frame(seq=seq, size=size, image_hash=image_hash, a11y=tuple(nodes))


class FakeClock:
    """Deterministic monotonic clock for StuckDetector/GuiLoop tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt
