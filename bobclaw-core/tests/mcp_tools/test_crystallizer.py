"""T1 FU2 — the bobclaw-side crystallizer (producer).

Turns tool_trace L4 evidence into servable ShortcutProfiles: qualifies on verified-success
floors, binds a capability-class, records per-project crystallization, and promotes to the
global tier on cross-project consensus. Anti-poison: a rejected-verdict 'success' doesn't count.
"""
from __future__ import annotations

from core.mcp_tools.crystallizer import crystallize
from core.mcp_tools.profile_consensus import ProfileConsensusLedger
from core.mcp_tools.profiles import ProfileStatus
from core.mcp_tools.reliability import RedisProfileCache

TS = "2026-07-04T00:00:00"


def _trace(job_shape, cls, *, success=True, verdict=None, tool_id="search", hsh="h1"):
    return {
        "job_shape": job_shape, "tool_id": tool_id, "schema_version_hash": hsh,
        "model_or_position": "worker-0", "capability_class": cls,
        "outcome": {"success": success, "verify_verdict": verdict},
        "ts": TS, "node_type": "tool_trace",
    }


async def _ledger(tmp_path):
    lg = ProfileConsensusLedger(tmp_path / "cons.db")
    await lg.init()
    return lg


async def test_crystallizes_qualifying_job_shape(tmp_path):
    cache = RedisProfileCache(redis_getter=None)  # in-process seam
    ledger = await _ledger(tmp_path)
    traces = [_trace("worker", "route_cheap") for _ in range(4)]  # 4 verified successes
    out = await crystallize(traces, "projA", cache, ledger,
                            now_iso=TS, global_promote_threshold=2, min_samples=3)
    assert out["crystallized"] == ["proj:projA:worker"]
    assert out["promoted_global"] == []  # only one project so far
    p = await cache.lookup("proj:projA:worker")
    assert p is not None
    assert p.capability_class == "route_cheap"
    assert p.status is ProfileStatus.CRYSTALLIZED
    assert p.stats["n"] == 4 and p.stats["success_rate"] == 1.0
    assert p.preload_tool_refs == [("search", "h1")]


async def test_skips_below_min_samples(tmp_path):
    cache = RedisProfileCache(redis_getter=None)
    ledger = await _ledger(tmp_path)
    out = await crystallize([_trace("worker", "route_cheap") for _ in range(2)], "projA",
                            cache, ledger, now_iso=TS, global_promote_threshold=2, min_samples=3)
    assert out["crystallized"] == []
    assert await cache.lookup("proj:projA:worker") is None


async def test_skips_below_min_success_rate(tmp_path):
    cache = RedisProfileCache(redis_getter=None)
    ledger = await _ledger(tmp_path)
    # 1 verified + 3 genuine failures ⇒ rate 0.25 < 2/3
    traces = [_trace("worker", "route_cheap")] + [
        _trace("worker", "route_cheap", success=False) for _ in range(3)
    ]
    out = await crystallize(traces, "projA", cache, ledger,
                            now_iso=TS, global_promote_threshold=2, min_samples=3)
    assert out["crystallized"] == []


async def test_anti_poison_rejected_verdict_does_not_crystallize(tmp_path):
    cache = RedisProfileCache(redis_getter=None)
    ledger = await _ledger(tmp_path)
    # success=True but the verifier VIOLATED them ⇒ not verified ⇒ must not crystallize
    traces = [_trace("worker", "route_cheap", success=True, verdict="violated") for _ in range(5)]
    out = await crystallize(traces, "projA", cache, ledger,
                            now_iso=TS, global_promote_threshold=2, min_samples=3)
    assert out["crystallized"] == []


async def test_promotes_to_global_after_threshold_projects(tmp_path):
    cache = RedisProfileCache(redis_getter=None)
    ledger = await _ledger(tmp_path)
    traces = [_trace("worker", "route_cheap") for _ in range(4)]
    await crystallize(traces, "projA", cache, ledger,
                      now_iso=TS, global_promote_threshold=2, min_samples=3)
    out = await crystallize(traces, "projB", cache, ledger,
                            now_iso=TS, global_promote_threshold=2, min_samples=3)
    # crystallized in 2 distinct projects ⇒ promote to global
    assert "global:worker" in out["promoted_global"]
    g = await cache.lookup("global:worker")
    assert g is not None and g.capability_class == "route_cheap"


async def test_picks_best_class_when_multiple(tmp_path):
    cache = RedisProfileCache(redis_getter=None)
    ledger = await _ledger(tmp_path)
    # class A: 3/3 verified; class B: 1/3 verified ⇒ best = A
    traces = (
        [_trace("plan_code", "plan_code", tool_id="a") for _ in range(3)]
        + [_trace("plan_code", "synth_mid", tool_id="b")]
        + [_trace("plan_code", "synth_mid", tool_id="b", success=False) for _ in range(2)]
    )
    out = await crystallize(traces, "projA", cache, ledger,
                            now_iso=TS, global_promote_threshold=2, min_samples=3)
    assert out["crystallized"] == ["proj:projA:plan_code"]
    p = await cache.lookup("proj:projA:plan_code")
    assert p.capability_class == "plan_code"
