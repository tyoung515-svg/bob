"""T1 dynamic-adaptive MCP — shortcut profiles + the promotion gate (MS#4 · Slice 2, [D]/D-4/F1).

A shortcut profile maps a job-shape → (preloaded tools, capability-class, prompt scaffold,
outcome stats). A high-confidence CRYSTALLIZED profile skips retrieval; a miss falls back to the
chapter router and writes the outcome back. The spine is POISONABLE (model-generated success
self-reinforces, F1), so promotion is gated hard:

- **Probation:** a fresh profile is PROBATIONARY — it bypasses retrieval but is STILL fully
  validated, so a bad seed can't hide behind the fast path.
- **Consensus (D-4, F1-hardened):** promote to CRYSTALLIZED only on ≥2/3 HETEROGENEOUS votes
  INCLUDING at least one DETERMINISTIC check — a same-model-class N=3 consensus is the N=1 failure
  mode in disguise.
- **Schema-drift quarantine:** profiles pin ``(tool_id, schema_version_hash)``; at preload a hash
  mismatch → QUARANTINED (re-validate, never silently used/deleted).

Profiles bind to a CAPABILITY-CLASS, never a concrete model (survives a model swap). PURE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProfileStatus(str, Enum):
    OPEN = "open"
    PROBATIONARY = "probationary"
    CRYSTALLIZED = "crystallized"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class Vote:
    source: str            # the voter (a family / seat / a deterministic check id)
    passed: bool
    deterministic: bool = False


@dataclass
class ShortcutProfile:
    job_shape_key: str
    preload_tool_refs: list[tuple[str, str]]        # [(tool_id, schema_version_hash)]
    capability_class: str                           # NOT a concrete model
    prompt_scaffold: str = ""
    status: ProfileStatus = ProfileStatus.PROBATIONARY
    stats: dict = field(default_factory=lambda: {"n": 0, "success_rate": 0.0, "last_verified": None})

    def to_dict(self) -> dict:
        """JSON-safe dict (tuples → lists, status → its value) for the Redis cache (D-5)."""
        return {
            "job_shape_key": self.job_shape_key,
            "preload_tool_refs": [list(t) for t in self.preload_tool_refs],
            "capability_class": self.capability_class,
            "prompt_scaffold": self.prompt_scaffold,
            "status": self.status.value,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShortcutProfile":
        return cls(
            job_shape_key=d["job_shape_key"],
            preload_tool_refs=[tuple(t) for t in d.get("preload_tool_refs", [])],
            capability_class=d["capability_class"],
            prompt_scaffold=d.get("prompt_scaffold", ""),
            status=ProfileStatus(d.get("status", ProfileStatus.PROBATIONARY.value)),
            stats=d.get("stats") or {"n": 0, "success_rate": 0.0, "last_verified": None},
        )


def seed_probationary(
    job_shape_key: str,
    preload_tool_refs: list[tuple[str, str]],
    capability_class: str,
    *,
    prompt_scaffold: str = "",
) -> ShortcutProfile:
    """Seed a PROBATIONARY profile (v0 seeds come from the _PLAN_INTENT/_CODE_SHAPED regex priors).
    Probationary ⇒ bypasses retrieval but is still fully validated (F1)."""
    return ShortcutProfile(
        job_shape_key=job_shape_key, preload_tool_refs=list(preload_tool_refs),
        capability_class=capability_class, prompt_scaffold=prompt_scaffold,
        status=ProfileStatus.PROBATIONARY,
    )


def consensus_ok(votes: list[Vote]) -> bool:
    """D-4 / F1 promotion gate: ≥2/3 of votes pass, at least one PASSING vote is DETERMINISTIC,
    and the passing votes are HETEROGENEOUS (≥2 distinct sources). All three or no promotion."""
    if not votes:
        return False
    passing = [v for v in votes if v.passed]
    ratio_ok = len(passing) / len(votes) >= 2 / 3
    has_deterministic = any(v.deterministic for v in passing)
    heterogeneous = len({v.source for v in passing}) >= 2
    return ratio_ok and has_deterministic and heterogeneous


def promote(profile: ShortcutProfile, votes: list[Vote]) -> ShortcutProfile:
    """Promote a PROBATIONARY profile to CRYSTALLIZED iff the consensus gate passes; otherwise it
    stays probationary (a quarantined/open profile is never promoted here). Mutates + returns."""
    if profile.status is ProfileStatus.PROBATIONARY and consensus_ok(votes):
        profile.status = ProfileStatus.CRYSTALLIZED
    return profile


def check_schema_drift(profile: ShortcutProfile, current_hash_by_tool: dict[str, str]) -> bool:
    """Quarantine the profile if ANY pinned tool's schema hash != the current hash. Returns True
    iff it was quarantined (never silently used, never silently deleted)."""
    drifted = any(
        current_hash_by_tool.get(tid) != pinned for tid, pinned in profile.preload_tool_refs
    )
    if drifted:
        profile.status = ProfileStatus.QUARANTINED
    return drifted


__all__ = [
    "ProfileStatus", "Vote", "ShortcutProfile", "seed_probationary",
    "consensus_ok", "promote", "check_schema_drift",
]
