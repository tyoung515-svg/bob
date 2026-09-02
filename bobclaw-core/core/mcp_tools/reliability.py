"""T1 dynamic-adaptive MCP — reliability-routing (D-2) + the profile cache (MS#4 · Slice 3).

D-2: T1 SUBSUMES the old reliability-routing (M2). The telemetry triple's (model/position +
core/verify verdict) IS the per-(class, job-shape) reliability signal; ``best_class_for`` routes
a job shape to the capability-class with the best VERIFIED track record on THAT job shape.

The ``ProfileCache`` is the fast-path lookup: a CRYSTALLIZED (or still-validating PROBATIONARY)
profile is returned so the caller can skip retrieval; a QUARANTINED/OPEN profile is NEVER served
(schema-drift safety). Shared across processes in prod via Redis/Qdrant (SPEC D-5) — this is the
in-process seam. PURE.

NOTE: the ``route.py::_select_face`` fast-path wiring is DEFERRED — the SPEC (§8) leaves the
tool-profile-fast-path ↔ _select_face ↔ agent-token-pin-authority interaction as an open design
question; wiring it blind would risk the core routing heuristic. See LOOP-TRACKER (BLOCKED).
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

from core.mcp_tools.profiles import ProfileStatus, ShortcutProfile

logger = logging.getLogger(__name__)

# statuses whose profile the fast-path may serve (crystallized = skip retrieval; probationary =
# bypass retrieval but the caller still fully validates). Quarantined/open are never served.
_SERVABLE = (ProfileStatus.CRYSTALLIZED, ProfileStatus.PROBATIONARY)


class ProfileCache:
    """Job-shape → ShortcutProfile fast-path lookup (quarantine-aware)."""

    def __init__(self, profiles: Optional[Iterable[ShortcutProfile]] = None):
        self._by_key: dict[str, ShortcutProfile] = {}
        for p in profiles or ():
            self._by_key[p.job_shape_key] = p

    def put(self, profile: ShortcutProfile) -> None:
        self._by_key[profile.job_shape_key] = profile

    def lookup(self, job_shape_key: str) -> Optional[ShortcutProfile]:
        """Return a SERVABLE profile for the exact job-shape key, else None (caller retrieves).
        A quarantined/open profile is never returned — never silently used past a schema drift."""
        p = self._by_key.get(job_shape_key)
        return p if (p is not None and p.status in _SERVABLE) else None


# The Redis hash all cores share for T1 shortcut profiles (SPEC D-5). Field = the namespaced
# key (proj:<project>:<job_shape> / global:<job_shape>); value = the profile JSON.
_PROFILES_HKEY = "bobclaw:t1:profiles"


class RedisProfileCache:
    """Cross-process profile cache (SPEC D-5): a Redis hash shared by every core worker,
    with an in-process mirror as the FAIL-OPEN fallback (a Redis blip degrades to the local
    view; it never wedges routing). Keyed by the EXPLICIT namespaced key. Quarantine-aware —
    a non-servable profile is never returned (same guard as ``ProfileCache``).

    ``redis_getter`` is injected (tests pass a fake); ``None`` ⇒ in-process only (the pure
    seam). Serialization rides ``ShortcutProfile.to_dict``/``from_dict``.
    """

    def __init__(self, redis_getter=None):
        self._redis_getter = redis_getter
        self._local: dict[str, ShortcutProfile] = {}

    def _redis(self):
        return self._redis_getter() if self._redis_getter else None

    async def put(self, key: str, profile: ShortcutProfile) -> None:
        self._local[key] = profile
        r = self._redis()
        if r is None:
            return
        try:
            await r.hset(_PROFILES_HKEY, key, json.dumps(profile.to_dict()))
        except Exception:  # noqa: BLE001 — fail open; the local mirror still serves
            logger.debug("RedisProfileCache put failed for %s (non-fatal)", key, exc_info=True)

    async def lookup(self, key: str) -> Optional[ShortcutProfile]:
        """Servable profile for the namespaced key, else None. Prefers Redis (source of
        truth); falls back to the local mirror on any Redis error."""
        profile: Optional[ShortcutProfile] = None
        r = self._redis()
        if r is not None:
            try:
                raw = await r.hget(_PROFILES_HKEY, key)
                if raw is not None:
                    profile = ShortcutProfile.from_dict(json.loads(raw))
            except Exception:  # noqa: BLE001 — fall back to the local mirror
                profile = None
        if profile is None:
            profile = self._local.get(key)
        return profile if (profile is not None and profile.status in _SERVABLE) else None


def best_class_for(
    job_shape: str,
    traces: Iterable[dict],
    *,
    min_samples: int = 1,
) -> Optional[str]:
    """D-2 reliability-routing: the capability-class with the best VERIFIED success rate on
    ``job_shape`` (from ``tool_trace`` L4 payloads). A class needs ≥ ``min_samples`` traces to
    qualify. Ties break by class name (deterministic). None if no class qualifies.

    A trace counts as a verified success iff ``outcome.success`` AND the verify verdict is not a
    failure marker (a gated call that the verifier rejected does not count, even if it 'ran')."""
    agg: dict[str, list[int]] = {}
    for t in traces:
        if t.get("job_shape") != job_shape:
            continue
        cls = t.get("capability_class")
        if not cls:
            continue
        outcome = t.get("outcome") or {}
        verdict = (outcome.get("verify_verdict") or "").lower()
        verified = bool(outcome.get("success")) and verdict not in ("not_entailed", "violated", "failed")
        agg.setdefault(cls, []).append(1 if verified else 0)

    ranked = sorted(
        ((cls, sum(v) / len(v)) for cls, v in agg.items() if len(v) >= min_samples),
        key=lambda t: (-t[1], t[0]),
    )
    return ranked[0][0] if ranked else None


__all__ = ["ProfileCache", "RedisProfileCache", "best_class_for"]
