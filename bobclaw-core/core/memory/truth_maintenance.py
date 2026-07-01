"""
Truth-maintenance pipeline — the ONLY legal writer of Fact.confidence.

Hard Rule 15 (load-bearing): confidence is set only by
TruthMaintenancePipeline. No other code path may mutate a fact's
confidence fields. Every confidence-write site within this module is
annotated with ``# allowlisted-confidence-writer``.

Usage
-----
    pipeline = TruthMaintenancePipeline(fact_store, source_weights_path)
    updated = await pipeline.corroborate(fact_id, event, "user_assertion")
    updated = await pipeline.contradict(fact_id, event, "tool_output")
    updated = await pipeline.deprecate(fact_id, "superseded by fct_2")
"""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.memory.exceptions import TruthMaintenanceError
from core.memory.models import ConfidenceStub, Event, Fact

if TYPE_CHECKING:
    from core.memory.interfaces import FactStore


class TruthMaintenancePipeline:
    def __init__(
        self,
        fact_store: FactStore,
        source_weights_path: Path,
    ) -> None:
        self._fact_store = fact_store
        self._weights: dict[str, float] = _load_source_weights(source_weights_path)

    def source_weight(self, source_kind: str) -> float:
        w = self._weights.get(source_kind)
        if w is None:
            raise TruthMaintenanceError(
                f"unknown source_kind {source_kind!r}; "
                f"known: {sorted(self._weights)}"
            )
        return w

    async def corroborate(
        self, fact_id: str, event: Event, source_kind: str
    ) -> Fact:
        weight = self.source_weight(source_kind)
        fact = await self._fact_store.get(fact_id)

        new_confidence = replace(
            fact.confidence,
            alpha=fact.confidence.alpha + weight,
            last_corroboration_event_id=event.event_id,
            last_corroboration_ts=event.ts,
        )
        new_fact = replace(fact, confidence=new_confidence)
        # allowlisted-confidence-writer
        await self._fact_store.put(new_fact)
        return new_fact

    async def contradict(
        self, fact_id: str, event: Event, source_kind: str
    ) -> Fact:
        weight = self.source_weight(source_kind)
        fact = await self._fact_store.get(fact_id)

        new_confidence = replace(
            fact.confidence,
            beta=fact.confidence.beta + weight,
            last_corroboration_event_id=event.event_id,
            last_corroboration_ts=event.ts,
        )
        new_fact = replace(fact, confidence=new_confidence)
        # allowlisted-confidence-writer
        await self._fact_store.put(new_fact)
        return new_fact

    async def deprecate(self, fact_id: str, reason: str) -> Fact:
        fact = await self._fact_store.get(fact_id)

        new_confidence = replace(
            fact.confidence,
            rank="deprecated",
        )
        new_fact = replace(fact, confidence=new_confidence)
        # allowlisted-confidence-writer
        await self._fact_store.put(new_fact)
        return new_fact


def _load_source_weights(path: Path) -> dict[str, float]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    weights_raw = raw.get("weights", {})
    if not isinstance(weights_raw, dict):
        raise TruthMaintenanceError("source_weights.toml: [weights] must be a table")
    weights: dict[str, float] = {}
    for key, value in weights_raw.items():
        try:
            weights[key] = float(value)
        except (TypeError, ValueError):
            raise TruthMaintenanceError(
                f"source_weights.toml: weight {key!r} = {value!r} is not a number"
            )
    return weights
