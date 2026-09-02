"""Test-pipe — council spine harness (SPEC §2, §9 unit 6).

The council spine (SPEC §2): ``front → shaped council (SHAPES registry) → synthesize``
— the reasoning / non-executable spine (NO code execution, NO test scoring).

It REUSES the real council nodes: seat resolution runs through the actual
``panel_dispatch_node`` (SPEC §5's fusion/debate dispatch), and the SHAPES registry
holds the live node references (``core.nodes.council.council_node`` etc., asserted by
identity in the tests) — the sweep selects a shape BY NAME. Seat EXECUTION goes through
the pipe's fenced+metered seam so cost capture + the honesty fence apply uniformly; the
final reduce is a deterministic delta-join for the network-free scaffold (the live
graph's full ``synthesize_node`` + grounding is the deferred live-sweep integration).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from core.config import COUNCIL_DEFAULT_SEATS
from core.testpipe.shapes import get_shape
from core.testpipe.types import TestItem

SendFn = Callable[..., Awaitable[str]]

_COUNCIL_SYSTEM = (
    "You are one voice of a ForestOS council. Answer delta-only; surface your "
    "load-bearing assumptions and the strongest objection to your own position."
)


@dataclass
class CouncilResult:
    item_id: str
    config_name: str
    shape: str
    answer: str = ""
    seats: list = field(default_factory=list)          # [(posture, backend, text)]
    error: Optional[str] = None


def _reduce(answers: list) -> str:
    """Deterministic delta-join of seat answers → one synthesized answer."""
    parts = [f"[{posture}] {txt}".strip() for posture, _b, txt in answers if txt]
    return "\n\n".join(parts)


async def run_council_spine(
    item: TestItem,
    config,
    *,
    make_send: Callable[[str], SendFn],
    seats: Optional[list] = None,
) -> CouncilResult:
    """Run the shaped council for ONE reasoning item (SPEC §2).

    ``config.shape`` selects the shape by name from the SHAPES registry. fusion/debate
    resolve seats via the REUSED ``panel_dispatch_node`` then execute each seat on the
    fenced seam; sequential runs a delta-only chain (each seat sees the prior answer).
    Returns the synthesized answer + per-seat provenance."""
    shape = get_shape(config.shape or "fusion")
    seat_postures = list(seats or COUNCIL_DEFAULT_SEATS)
    result = CouncilResult(item_id=item.id, config_name=config.name, shape=shape.name)

    if shape.mode == "sequential":
        prior = ""
        answers = []
        for posture in seat_postures:
            user = item.prompt + (f"\n\nPRIOR VOICE:\n{prior}" if prior else "")
            try:
                txt = await make_send(f"seat:{posture}")(
                    [{"role": "system", "content": _COUNCIL_SYSTEM},
                     {"role": "user", "content": user}],
                    config.worker,
                )
            except Exception as exc:  # noqa: BLE001
                result.error = f"seat_failed: {exc}"
                txt = ""
            prior = txt or prior
            answers.append((posture, config.worker, txt))
        result.seats = answers
        result.answer = _reduce(answers)
        return result

    # fusion / debate — reuse the real panel dispatch to resolve seats + build the task.
    spec = shape.build_spec(seat_postures, context="")
    disp = shape.dispatch_node({"task": item.prompt, "council_spec": spec})
    resolved = (disp.get("council_spec") or {}).get("resolved_seats") or []
    panel_task = (disp.get("council_spec") or {}).get("panel_task") or item.prompt
    answers = []
    for seat in resolved:
        posture = seat.get("posture", "framer")
        backend = seat.get("backend", config.worker)
        try:
            txt = await make_send(f"seat:{posture}")(
                [{"role": "system", "content": _COUNCIL_SYSTEM},
                 {"role": "user", "content": panel_task}],
                backend,
            )
        except Exception as exc:  # noqa: BLE001
            result.error = f"seat_failed: {exc}"
            txt = ""
        answers.append((posture, backend, txt))
    result.seats = answers
    result.answer = _reduce(answers)
    return result
