"""sources.py — the thin FOREST source descriptor + catalog (MS9-F3, F0 reuse call).

F0 (`sprints/F0-RECONCILIATION.md` §3) ruled: REUSE the W1 ``SourcesRegistry`` SHAPE +
``WatchSchedule`` + ``dedupe.py`` as READ-ONLY imports, but OWN a thin forest-source descriptor —
because W1's ``WatchSource`` validators are web-bound (``_validate_url`` demands an ``http`` URL,
``_validate_kind`` restricts to a web kind set, ``_validate_auth_required`` hard-rejects authed) and
would fail-loud on a forest MEASUREMENT source (testpipe-uplift, flight/spend telemetry, eval-results
file, LKS corpus stats — none are http URLs, none in that kind set).

So this module mirrors ``core.watch.registry.SourcesRegistry``'s shape (id/kind/enabled +
``schedule_profile``-style envelope emission) for forest measurement sources, while reusing the W1
``WatchSchedule`` model VERBATIM (read-only import) for cron/task validation. It is a CATALOG only:
the RECORD of measurement is program-ledger TRUTH (``store.py`` / ``events.py``), never a registry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Read-only reuse of the W1 cron/task validator (F0 §3: ride WatchSchedule as-is).
from core.watch.registry import WatchSchedule

SUPPORTED_VERSION = 0

# Forest measurement source kinds v0 (SPEC §2 / §5). NOT the web kind set — these are the
# fixture-driven measurement sources F3 resolves (inv. 13: stubs/fixtures only, no live runs).
FOREST_SOURCE_KINDS = frozenset({
    "testpipe_uplift",   # the testpipe-uplift adapter STUB (SPEC §5 seed program)
    "flight_spend",      # flight/spend telemetry, read-only reader
    "eval_results",      # SES / eval-results file reader
    "lks_stats",         # LKS corpus stats
})


class ForestSource(BaseModel):
    """One forest measurement source descriptor — feeds a hypothesis node's ``measurement_source``.

    A source declares WHICH hypothesis node it measures (``node_id``), WHAT metric it emits
    (``metric``), and any source-specific fixture wiring (``config``, e.g. a fixture path). It is
    NOT a web watch source: no url / auth / web-kind fields (F0 §3).
    """

    id: str
    kind: str
    node_id: str
    metric: str
    enabled: bool = True
    unit: str | None = None
    weight: int | float = 1
    config: dict = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("id must be non-empty")
        return v

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in FOREST_SOURCE_KINDS:
            raise ValueError(
                f"kind {v!r} is not a valid forest source kind; allowed: {sorted(FOREST_SOURCE_KINDS)}"
            )
        return v

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("node_id must be non-empty (a source measures a hypothesis node)")
        return v

    @field_validator("metric")
    @classmethod
    def _validate_metric(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("metric must be non-empty")
        return v


class ForestSourcesRegistry(BaseModel):
    """A validated CATALOG of forest measurement sources (mirrors W1 ``SourcesRegistry`` shape)."""

    version: int
    sources: list[ForestSource]
    schedule: WatchSchedule | None = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: int) -> int:
        if v != SUPPORTED_VERSION:
            raise ValueError(f"version must be {SUPPORTED_VERSION}, got {v} (unsupported)")
        return v

    @model_validator(mode="after")
    def _validate_sources(self) -> "ForestSourcesRegistry":
        if not self.sources:
            raise ValueError("sources cannot be empty")
        seen: set[str] = set()
        dups: list[str] = []
        for src in self.sources:
            if src.id in seen:
                dups.append(src.id)
            seen.add(src.id)
        if dups:
            raise ValueError(f"duplicate source ids: {dups}")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "ForestSourcesRegistry":
        """Load + validate a forest sources registry from a YAML file."""
        with Path(path).open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ValueError("YAML root must be a mapping")
        return cls.model_validate(raw)

    def get(self, source_id: str) -> ForestSource:
        """Look up a source by id. Raises ``KeyError`` if not found."""
        for src in self.sources:
            if src.id == source_id:
                return src
        raise KeyError(f"unknown forest source id: {source_id}")

    def ids(self) -> list[str]:
        """Return source ids in file order."""
        return [src.id for src in self.sources]

    def enabled_sources(self) -> list[ForestSource]:
        """Return only enabled sources."""
        return [src for src in self.sources if src.enabled]

    def schedule_profile(
        self,
        name: str,
        *,
        cron: str | None = None,
        task: str | None = None,
        face_hint: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Build a P5 scheduler profile envelope binding this catalog to a cron.

        Mirrors ``core.watch.registry.SourcesRegistry.schedule_profile``: fills missing
        cron/task/face_hint/owner from ``self.schedule`` when present, VALIDATES the resulting
        schedule via the reused ``WatchSchedule``, and returns ``{"name":..., "schedule":{...}}`` —
        exactly the shape ``core.scheduler.run_tick`` reads.
        """
        if cron is None:
            if self.schedule is None:
                raise ValueError("cron is required when no global schedule is set")
            cron = self.schedule.cron
        if task is None:
            if self.schedule is None:
                raise ValueError("task is required when no global schedule is set")
            task = self.schedule.task
        if face_hint is None and self.schedule is not None:
            face_hint = self.schedule.face_hint
        if owner is None and self.schedule is not None:
            owner = self.schedule.owner

        try:
            WatchSchedule(cron=cron, task=task, face_hint=face_hint, owner=owner)
        except Exception as e:  # noqa: BLE001 — surface a clear catalog-level error
            raise ValueError(f"invalid schedule profile: {e}") from e

        return {
            "name": name,
            "schedule": {
                "cron": cron,
                "task": task,
                "face_hint": face_hint,
                "owner": owner,
            },
        }


__all__ = [
    "SUPPORTED_VERSION",
    "FOREST_SOURCE_KINDS",
    "ForestSource",
    "ForestSourcesRegistry",
]
