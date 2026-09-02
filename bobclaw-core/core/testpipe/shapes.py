"""Test-pipe — the SHAPES registry (SPEC §5, §9 unit 6).

``SHAPES = { name → ShapeSpec }`` mirroring the backend-string / teams convention:
the sweep references a council shape BY NAME, and adding a future shape (tournament,
self-repair-council, chair-arbitrated) is a single :func:`register` call — it then
becomes a selectable test slot with NO harness change (SPEC §5).

The three shipped shapes REUSE the existing council nodes BY REFERENCE — the
registry holds the actual callables from ``core.nodes.panel`` / ``core.nodes.council``
/ ``core.nodes.debate`` (asserted by identity in the tests), so these are the live
nodes, never shadow copies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from core.nodes.council import council_node
from core.nodes.debate import debate_converge_node
from core.nodes.panel import panel_dispatch_node, panel_worker_node
from core.nodes.synthesize import synthesize_node


@dataclass(frozen=True)
class ShapeSpec:
    """A selectable council shape (SPEC §5).

    ``mode`` is the ``council_spec["mode"]`` the existing nodes key on. The node
    fields hold the REUSED callables so a caller drives the real fusion/debate
    fan-out (dispatch → seat×N → close) or the sequential chain, never a copy.
    ``params`` carries shape knobs (e.g. debate ``max_rounds``)."""

    name: str
    mode: str
    dispatch_node: Optional[Callable] = None
    seat_node: Optional[Callable] = None
    close_node: Optional[Callable] = None
    sequential_node: Optional[Callable] = None
    params: dict = field(default_factory=dict)

    def build_spec(self, seats: list[str], **overrides) -> dict:
        """A ``council_spec`` for this shape (seats + mode + params). The pipe passes
        it to the reused dispatch node exactly as the live graph does."""
        spec: dict = {"mode": self.mode, "seats": list(seats)}
        spec.update(self.params)
        spec.update(overrides)
        return spec


# ── the live registry (SPEC §5: ships the three that already exist as nodes) ──
SHAPES: dict[str, ShapeSpec] = {}


def register(spec: ShapeSpec, *, overwrite: bool = False) -> ShapeSpec:
    """Register a shape → it is immediately a selectable test slot (SPEC §5).

    Rejects a duplicate name unless ``overwrite`` (so a typo can't silently shadow a
    shipped shape). This is the WHOLE extension surface — no harness edit needed."""
    if spec.name in SHAPES and not overwrite:
        raise ValueError(f"shape {spec.name!r} already registered")
    SHAPES[spec.name] = spec
    return spec


def get_shape(name: str) -> ShapeSpec:
    """Resolve a shape by name; :class:`KeyError` (loud) on an unknown shape."""
    if name not in SHAPES:
        raise KeyError(f"unknown shape {name!r}; registered: {sorted(SHAPES)}")
    return SHAPES[name]


def shape_names() -> list[str]:
    return sorted(SHAPES)


# fusion (SPEC §5 → core/nodes/panel.py): all seats answer blind, synthesize closes.
register(ShapeSpec(
    name="fusion", mode="fusion",
    dispatch_node=panel_dispatch_node, seat_node=panel_worker_node,
    close_node=synthesize_node,
))
# sequential (SPEC §5 → core/nodes/council.py): the native Claude→Gemini→synth chain.
register(ShapeSpec(
    name="sequential", mode="sequential",
    sequential_node=council_node,
))
# debate (SPEC §5 → core/nodes/debate.py, param max_rounds): round-robin to convergence.
register(ShapeSpec(
    name="debate", mode="debate",
    dispatch_node=panel_dispatch_node, seat_node=panel_worker_node,
    close_node=debate_converge_node, params={"max_rounds": 3},
))
