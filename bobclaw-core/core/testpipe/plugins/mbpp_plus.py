"""Test-pipe — MBPP+ (EvalPlus) plugin shell (SPEC §9 unit 1: "add MBPP+ as second plugin").

Mirrors :mod:`humaneval_plus` — the SECOND benchmark plugin proving the interface is
extensible (SPEC §1). ``evalplus`` is imported LAZILY; the live dataset load is the
deferred live sweep (Travis's, SPEC §10).
"""
from __future__ import annotations

from typing import Optional

from core.testpipe.loader import extract_doctests, extract_signature
from core.testpipe.plugins import EvalPlusNotAvailable
from core.testpipe.types import SPINE_BUILD, TestItem

_INSTALL_HINT = (
    "MBPP+ requires the 'evalplus' package (not a bobclaw dependency). Install it in "
    "the live-sweep env: `uv pip install evalplus` — the LIVE sweep is Travis's "
    "(SPEC §10), deferred; MS5-P1 proves the plugin INTERFACE only."
)


class MbppPlusPlugin:
    """:class:`~core.testpipe.loader.BenchmarkPlugin` over EvalPlus MBPP+."""

    name = "mbpp_plus"
    spine = SPINE_BUILD

    def load(self, limit: Optional[int] = None) -> list[TestItem]:
        try:
            from evalplus.data import get_mbpp_plus  # type: ignore
        except ImportError as exc:  # pragma: no cover - evalplus not installed here
            raise EvalPlusNotAvailable(_INSTALL_HINT) from exc

        raw = get_mbpp_plus()  # pragma: no cover - requires the dataset
        items: list[TestItem] = []
        for task_id, task in raw.items():  # pragma: no cover - requires the dataset
            prompt = task["prompt"]
            entry = task["entry_point"]
            items.append(TestItem(
                id=str(task_id),
                prompt=prompt,
                entry_point=entry,
                visible_tests=extract_doctests(prompt, entry),
                hidden_tests=(),  # held by the official EvalPlus scorer (§7.2)
                signature=extract_signature(prompt),
                canonical_solution=task.get("canonical_solution", ""),
            ))
            if limit is not None and len(items) >= limit:
                break
        return items
