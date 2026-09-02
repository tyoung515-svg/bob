"""T1 fast-path — the bobclaw-side crystallizer (FU2, the MISSING producer).

The T1 consumer (``profile_routing`` two-tier lookup + ε valve + class→face + the route_node
fast-path) is fully built, but NOTHING populates ``bobclaw:t1:profiles`` — so with
``T1_FASTPATH_ENABLED`` on, every lookup misses and routing stays byte-identical. This module
is the producer: it turns recorded ``tool_trace`` L4 evidence into servable ``ShortcutProfile``s.

Pipeline per (job_shape, project):
  best_class_for(verified-success-filtered)  →  a CRYSTALLIZED ShortcutProfile
  →  RedisProfileCache.put("proj:<project>:<job_shape>")
  →  ProfileConsensusLedger.record_crystallization(job_shape, project)
  →  when it has crystallized in ≥ threshold DISTINCT projects, ALSO put "global:<job_shape>".

**Anti-poison (F1):** confidence rides ``best_class_for``'s VERIFIED-success filter (a gated
call the verifier rejected does NOT count), plus ``min_samples`` / ``min_success_rate`` floors —
never the model's own say-so. Profiles bind a CAPABILITY-CLASS, never a concrete model.

PURE w.r.t. IO: the cache, ledger, and clock are all injected. This ONLY populates the cache —
it never flips ``T1_FASTPATH_ENABLED`` (that stays OFF until Gate B).
"""
from __future__ import annotations

import logging

from core.mcp_tools.profile_consensus import ProfileConsensusLedger
from core.mcp_tools.profile_routing import global_key, namespaced_key
from core.mcp_tools.profiles import ProfileStatus, ShortcutProfile
from core.mcp_tools.reliability import RedisProfileCache, best_class_for

logger = logging.getLogger(__name__)

# Verify verdicts that DISQUALIFY a trace from counting as a verified success (mirrors
# reliability.best_class_for so the crystallizer's rate matches its class choice exactly).
_FAILURE_VERDICTS = ("not_entailed", "violated", "failed")


def _is_verified(trace: dict) -> bool:
    outcome = trace.get("outcome") or {}
    verdict = (outcome.get("verify_verdict") or "").lower()
    return bool(outcome.get("success")) and verdict not in _FAILURE_VERDICTS


def _verified_rate(job_shape: str, cls: str, traces: list[dict]) -> tuple[float, int]:
    """Verified-success rate + sample count for (job_shape, cls). Matches best_class_for."""
    vals = [
        1 if _is_verified(t) else 0
        for t in traces
        if t.get("job_shape") == job_shape and t.get("capability_class") == cls
    ]
    return (sum(vals) / len(vals), len(vals)) if vals else (0.0, 0)


def _preload_refs(job_shape: str, cls: str, traces: list[dict]) -> list[tuple[str, str]]:
    """Distinct (tool_id, schema_version_hash) from VERIFIED traces of (job_shape, cls) —
    what the fast-path would preload. Last hash per tool wins; sorted for determinism."""
    seen: dict[str, str] = {}
    for t in traces:
        if t.get("job_shape") != job_shape or t.get("capability_class") != cls:
            continue
        if not _is_verified(t):
            continue
        tid = t.get("tool_id")
        if tid:
            seen[tid] = t.get("schema_version_hash") or ""
    return sorted(seen.items())


async def crystallize(
    traces: list[dict],
    project: str,
    cache: RedisProfileCache,
    ledger: ProfileConsensusLedger,
    *,
    now_iso: str,
    global_promote_threshold: int,
    min_samples: int = 3,
    min_success_rate: float = 2 / 3,
) -> dict:
    """Crystallize servable profiles from a batch of ``tool_trace`` L4 payloads for ``project``.

    A job_shape qualifies when its best VERIFIED capability-class has ``>= min_samples`` traces
    and a verified-success rate ``>= min_success_rate``. Returns
    ``{crystallized: [proj_key, ...], promoted_global: [global_key, ...]}``.
    """
    crystallized: list[str] = []
    promoted_global: list[str] = []
    job_shapes = sorted({t.get("job_shape") for t in traces if t.get("job_shape")})

    for js in job_shapes:
        best = best_class_for(js, traces, min_samples=min_samples)
        if best is None:
            continue
        rate, n = _verified_rate(js, best, traces)
        if n < min_samples or rate < min_success_rate:
            continue
        refs = _preload_refs(js, best, traces)
        stats = {"n": n, "success_rate": round(rate, 4), "last_verified": now_iso}

        proj_key = namespaced_key(js, project)
        await cache.put(proj_key, ShortcutProfile(
            job_shape_key=proj_key, preload_tool_refs=refs, capability_class=best,
            status=ProfileStatus.CRYSTALLIZED, stats=dict(stats),
        ))
        crystallized.append(proj_key)
        await ledger.record_crystallization(js, project, now_iso)
        logger.info("crystallized %s -> class=%s (n=%d rate=%.2f)", proj_key, best, n, rate)

        if await ledger.should_promote_global(js, global_promote_threshold):
            gkey = global_key(js)
            await cache.put(gkey, ShortcutProfile(
                job_shape_key=gkey, preload_tool_refs=refs, capability_class=best,
                status=ProfileStatus.CRYSTALLIZED, stats=dict(stats),
            ))
            promoted_global.append(gkey)
            logger.info("promoted %s to global tier (>= %d projects)", js, global_promote_threshold)

    return {"crystallized": crystallized, "promoted_global": promoted_global}


__all__ = ["crystallize"]
