"""T1 flight-aware routing (Lane 1b) — namespacing, ε valve, Redis cache, consensus ledger.

(Under tests/telemetry/ alongside the flight substrate; the T1 lane consumes it.)
"""
from __future__ import annotations

from core.config import config
from core.mcp_tools.profile_consensus import ProfileConsensusLedger
from core.mcp_tools.profile_routing import (
    JOB_CHAT,
    JOB_PLAN_CODE,
    JOB_PLAN_CONCEPT,
    JOB_WORKER,
    class_to_face,
    epsilon_should_explore,
    global_key,
    job_shape_for,
    lookup_two_tier,
    namespaced_key,
    project_of,
    resolve_fastpath_face,
)
from core.mcp_tools.profiles import ProfileStatus, ShortcutProfile, seed_probationary
from core.mcp_tools.reliability import RedisProfileCache

TS = "2026-07-04T00:00:00"


def _profile(cls="worker", status=ProfileStatus.CRYSTALLIZED):
    p = seed_probationary("k", [("t", "h")], cls)
    p.status = status
    return p


# ── job-shape derivation ──────────────────────────────────────────────────────
def test_job_shape_for():
    assert job_shape_for({"dispatch_subtask": {"x": 1}}) == JOB_WORKER
    assert job_shape_for({"phase": "build"}) == JOB_WORKER
    assert job_shape_for({"task": "plan a refactor of the auth module"}) == JOB_PLAN_CODE
    assert job_shape_for({"task": "outline the hiring plan"}) == JOB_PLAN_CONCEPT
    assert job_shape_for({"task": "what's the weather"}) == JOB_CHAT       # no plan-intent
    assert job_shape_for({"task": "refactor the auth module"}) == JOB_CHAT  # code but no plan-intent (mirrors _select_face)


def test_project_of_and_keys():
    assert project_of({}) == "default"
    assert project_of({"project": "  sps  "}) == "sps"
    assert namespaced_key("chat", "sps") == "proj:sps:chat"
    assert global_key("chat") == "global:chat"


# ── ε valve ───────────────────────────────────────────────────────────────────
def test_epsilon_valve():
    assert epsilon_should_explore(0.10, 0.05) is True    # roll under rate ⇒ explore
    assert epsilon_should_explore(0.10, 0.50) is False
    assert epsilon_should_explore(0.0, 0.0) is False      # never explore at 0
    assert epsilon_should_explore(2.0, 0.99) is True      # clamps >1 → always explore


# ── class → face ──────────────────────────────────────────────────────────────
def test_class_to_face():
    assert class_to_face("worker") == "worker-deepseek"
    assert class_to_face("plan_code") == "planner-kimi"
    assert class_to_face("nonsense_class") is None        # unknown ⇒ heuristic
    assert class_to_face(None) is None


# ── two-tier lookup + Redis cache (in-process fail-open) ──────────────────────
async def test_redis_cache_inprocess_and_servable_filter():
    cache = RedisProfileCache(redis_getter=None)  # in-process only
    await cache.put("proj:sps:chat", _profile(status=ProfileStatus.CRYSTALLIZED))
    await cache.put("proj:sps:worker", _profile(status=ProfileStatus.QUARANTINED))
    assert await cache.lookup("proj:sps:chat") is not None
    assert await cache.lookup("proj:sps:worker") is None   # quarantined never served
    assert await cache.lookup("proj:sps:missing") is None


async def test_two_tier_prefers_project_then_global():
    cache = RedisProfileCache(redis_getter=None)
    await cache.put(global_key("chat"), _profile(cls="worker"))
    # only global present → global hit
    assert (await lookup_two_tier(cache, "chat", "sps")).capability_class == "worker"
    # project-specific present → it wins
    await cache.put(namespaced_key("chat", "sps"), _profile(cls="plan_code"))
    assert (await lookup_two_tier(cache, "chat", "sps")).capability_class == "plan_code"


class _FakeHash:
    def __init__(self):
        self.h = {}

    async def hset(self, key, field, val):
        self.h.setdefault(key, {})[field] = val

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)


class _BoomHash:
    async def hset(self, *a, **k):
        raise RuntimeError("down")

    async def hget(self, *a, **k):
        raise RuntimeError("down")


async def test_redis_cache_roundtrips_via_fake_redis():
    fake = _FakeHash()
    cache = RedisProfileCache(redis_getter=lambda: fake)
    await cache.put("proj:sps:chat", _profile(cls="synth_deep"))
    # a FRESH cache instance sees it through Redis (cross-process)
    cache2 = RedisProfileCache(redis_getter=lambda: fake)
    got = await cache2.lookup("proj:sps:chat")
    assert got is not None and got.capability_class == "synth_deep"


async def test_redis_cache_fails_open_to_local():
    cache = RedisProfileCache(redis_getter=lambda: _BoomHash())
    await cache.put("proj:sps:chat", _profile())   # Redis write fails → local mirror holds
    assert await cache.lookup("proj:sps:chat") is not None  # served from local on Redis error


# ── resolve_fastpath_face ─────────────────────────────────────────────────────
async def test_resolve_fastpath_face_hit():
    cache = RedisProfileCache(redis_getter=None)
    await cache.put(namespaced_key("chat", "default"), _profile(cls="worker"))
    face = await resolve_fastpath_face(
        {"task": "hi", "face_id": "assistant"}, cache, epsilon=0.0, roll=lambda: 0.9,
    )
    assert face == "worker-deepseek"


async def test_resolve_fastpath_face_epsilon_explores():
    cache = RedisProfileCache(redis_getter=None)
    await cache.put(namespaced_key("chat", "default"), _profile(cls="worker"))
    face = await resolve_fastpath_face(
        {"task": "hi", "face_id": "assistant"}, cache, epsilon=1.0, roll=lambda: 0.0,
    )
    assert face is None   # ε=1 ⇒ always explore ⇒ fall back


async def test_resolve_fastpath_face_no_profile():
    cache = RedisProfileCache(redis_getter=None)
    face = await resolve_fastpath_face(
        {"task": "hi", "face_id": "assistant"}, cache, epsilon=0.0, roll=lambda: 0.9,
    )
    assert face is None


async def test_resolve_fastpath_face_same_face_noop():
    cache = RedisProfileCache(redis_getter=None)
    await cache.put(namespaced_key("chat", "default"), _profile(cls="worker"))
    # current face already worker-deepseek ⇒ no swap
    face = await resolve_fastpath_face(
        {"task": "hi", "face_id": "worker-deepseek"}, cache, epsilon=0.0, roll=lambda: 0.9,
    )
    assert face is None


# ── consensus ledger ──────────────────────────────────────────────────────────
async def _new_ledger(tmp_path):
    led = ProfileConsensusLedger(tmp_path / "t1.db")
    await led.init()
    return led


async def test_consensus_counts_distinct_projects(tmp_path):
    led = await _new_ledger(tmp_path)
    assert await led.record_crystallization("chat", "sps", TS) is True
    assert await led.record_crystallization("chat", "sps", TS) is False   # repeat ⇒ no-op
    assert await led.record_crystallization("chat", "unwinding", TS) is True
    assert await led.project_count("chat") == 2


async def test_consensus_threshold(tmp_path):
    led = await _new_ledger(tmp_path)
    await led.record_crystallization("chat", "sps", TS)
    assert await led.should_promote_global("chat", config.T1_GLOBAL_PROMOTE_PROJECTS) is False
    await led.record_crystallization("chat", "unwinding", TS)
    assert await led.should_promote_global("chat", 2) is True
