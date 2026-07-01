"""JOAT v1 — tests for the LIVE health-walk probe (``core/health_probe.py``).

Covers the three build-pipe-verified pure primitives (incl. the edge cases the
generated tests didn't), the backend→client dispatch coverage guard (anti-drift vs
``execute._default_send_to_backend``), the throttle-pin gate, the short-TTL
reachability cache, fail-open everywhere, and — the JOAT v1 acceptance — that
``teams.resolve`` walks the escalation chain when wired to the real live probe with
fake clients/pins.

No network: the backend clients are never constructed; ``_client_health`` /
``_make_client`` / the execute pin+router helpers are injected.
"""
from __future__ import annotations

import asyncio

import pytest

from core import health_probe as hp
from core import teams
from core.config import KNOWN_BACKENDS
from core.faces.registry import Face


@pytest.fixture(autouse=True)
def _isolate():
    """Reset the process-local cache + restore the team probe between tests."""
    hp.reset_health_cache()
    original = teams._health_probe
    teams.set_active_team(None)
    yield
    teams.set_health_probe(original)
    teams.set_active_team(None)
    hp.reset_health_cache()


def _face(**kw) -> Face:
    base = dict(id="t", name="T", system_prompt="p")
    base.update(kw)
    return Face(**base)


# ── build-pipe-verified pure primitives (+ the uncovered edges) ─────────────────

def test_walk_to_available_first_truthy_wins():
    assert hp.walk_to_available(["a", "b", "c"], {"a": False, "b": True, "c": True}) == "b"


def test_walk_to_available_whole_chain_down_returns_primary():
    assert hp.walk_to_available(["a", "b"], {"a": False, "b": False}) == "a"


def test_walk_to_available_unknown_candidate_is_available():
    # Fail-open: a candidate missing from the map is treated available.
    assert hp.walk_to_available(["x", "y"], {}) == "x"
    assert hp.walk_to_available(["x", "y"], {"x": False}) == "y"


def test_walk_to_available_empty_raises():
    with pytest.raises(ValueError):
        hp.walk_to_available([], {})


def test_is_backend_available_throttle_overrides_healthy():
    assert hp.is_backend_available(throttled=True, healthy=True) is False
    assert hp.is_backend_available(throttled=False, healthy=True) is True
    assert hp.is_backend_available(throttled=False, healthy=False) is False


def test_cache_is_fresh_strictly_before_expiry():
    assert hp.cache_is_fresh(100.0, 99.9) is True
    assert hp.cache_is_fresh(100.0, 100.0) is False   # exactly-equal ⇒ stale
    assert hp.cache_is_fresh(100.0, 100.1) is False


# ── anti-drift: the probe covers every known backend ────────────────────────────

def test_probe_covers_every_known_backend():
    """Adding a backend to the dispatch map without teaching the probe must fail
    loudly here (mirrors execute._default_send_to_backend)."""
    missing = set(KNOWN_BACKENDS) - set(hp.COVERED_BACKENDS)
    assert not missing, f"health probe missing dispatch for: {sorted(missing)}"


# ── reachability dispatch (_client_health), clients injected ────────────────────

class _FakeClient:
    def __init__(self, ok: bool):
        self._ok = ok

    async def health_check(self) -> bool:
        return self._ok


@pytest.mark.asyncio
async def test_client_health_http_backend_uses_client(monkeypatch):
    monkeypatch.setattr(hp, "_make_client", lambda b: _FakeClient(ok=True))
    assert await hp._client_health("deepseek_v4_flash") is True
    monkeypatch.setattr(hp, "_make_client", lambda b: _FakeClient(ok=False))
    assert await hp._client_health("deepseek_v4_flash") is False


@pytest.mark.asyncio
async def test_client_health_local_uses_router(monkeypatch):
    import core.nodes.execute as ex

    class _Router:
        def __init__(self, best):
            self._best = best

        async def discover(self):
            return ["whatever"]

        def get_best_backend(self, _):
            return self._best

    monkeypatch.setattr(ex, "_router", _Router(best="ollama"))
    assert await hp._client_health("local") is True
    monkeypatch.setattr(ex, "_router", _Router(best=None))
    assert await hp._client_health("local") is False


@pytest.mark.asyncio
async def test_client_health_opencode_and_unknown_fail_open():
    assert await hp._client_health("opencode_serve") is True
    assert await hp._client_health("totally-unknown-backend") is True


# ── throttle-pin gate (execute._check_escalation_pin), uncached/live ────────────

@pytest.mark.asyncio
async def test_is_throttled_reads_pin_live(monkeypatch):
    import core.nodes.execute as ex

    async def pinned(_b):
        return "deepseek_v4_flash"   # a pin target ⇒ throttled

    async def unpinned(_b):
        return None

    monkeypatch.setattr(ex, "_check_escalation_pin", pinned)
    assert await hp._is_throttled("kimi_code") is True
    monkeypatch.setattr(ex, "_check_escalation_pin", unpinned)
    assert await hp._is_throttled("kimi_code") is False


@pytest.mark.asyncio
async def test_is_throttled_fail_open_on_redis_error(monkeypatch):
    import core.nodes.execute as ex

    async def boom(_b):
        raise RuntimeError("redis down")

    monkeypatch.setattr(ex, "_check_escalation_pin", boom)
    assert await hp._is_throttled("kimi_code") is False   # error ⇒ not throttled


# ── reachability cache: TTL freshness + forced re-probe ─────────────────────────

@pytest.mark.asyncio
async def test_cached_health_memoizes_within_ttl(monkeypatch):
    calls = {"n": 0}

    async def counting(_b):
        calls["n"] += 1
        return True

    monkeypatch.setattr(hp, "_client_health", counting)
    assert await hp._cached_health("glm_5_2") is True
    assert await hp._cached_health("glm_5_2") is True
    assert calls["n"] == 1   # second call served from cache


@pytest.mark.asyncio
async def test_cached_health_reprobes_after_expiry(monkeypatch):
    calls = {"n": 0}

    async def counting(_b):
        calls["n"] += 1
        return True

    monkeypatch.setattr(hp, "_client_health", counting)
    await hp._cached_health("glm_5_2")
    # Force the entry stale (expiry in the past) — the cache_is_fresh gate must re-probe.
    _expiry, val = hp._health_cache["glm_5_2"]
    hp._health_cache["glm_5_2"] = (-1.0, val)
    await hp._cached_health("glm_5_2")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_cached_health_fail_open_on_raise(monkeypatch):
    async def boom(_b):
        raise RuntimeError("boom")

    monkeypatch.setattr(hp, "_client_health", boom)
    assert await hp._cached_health("glm_5_2") is True   # raise ⇒ available


@pytest.mark.asyncio
async def test_cached_health_fail_open_on_timeout(monkeypatch):
    async def hang(_b):
        await asyncio.sleep(10)
        return False

    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_TIMEOUT", "0.05")
    monkeypatch.setattr(hp, "_client_health", hang)
    assert await hp._cached_health("glm_5_2") is True   # timeout ⇒ available


# ── A3 (JOAT v1.1): Redis-shared reachability cache (opt-in, fail-open) ──────────

class _FakeRedis:
    """Minimal async Redis double: dict-backed get/set, optional failure injection,
    and a record of set() calls (for the TTL assertion)."""

    def __init__(self, seed=None, fail=False):
        self.store = dict(seed or {})
        self.fail = fail
        self.set_calls = []

    async def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value
        self.set_calls.append((key, value, ex))


def _wire_fake_redis(monkeypatch, fake):
    import core.nodes.execute as ex
    monkeypatch.setattr(ex, "_get_redis", lambda: fake)


def test_redis_share_off_by_default(monkeypatch):
    monkeypatch.delenv("BOBCLAW_HEALTH_PROBE_REDIS", raising=False)
    assert hp._redis_share_on() is False
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    assert hp._redis_share_on() is True
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "0")
    assert hp._redis_share_on() is False


@pytest.mark.asyncio
async def test_cached_health_redis_share_off_never_touches_redis(monkeypatch):
    """Default (env unset): Redis is never consulted — a raising _get_redis proves it."""
    monkeypatch.delenv("BOBCLAW_HEALTH_PROBE_REDIS", raising=False)

    def _boom():
        raise AssertionError("Redis must not be used when sharing is off")

    import core.nodes.execute as ex
    monkeypatch.setattr(ex, "_get_redis", _boom)
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(True))
    assert await hp._cached_health("glm_5_2") is True   # process-local only


@pytest.mark.asyncio
async def test_cached_health_redis_hit_skips_probe(monkeypatch):
    """A sibling worker's shared 'healthy' is reused — the local probe is NOT called."""
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    fake = _FakeRedis(seed={hp._health_key("glm_5_2"): "1"})
    _wire_fake_redis(monkeypatch, fake)
    calls = {"n": 0}

    async def counting(_b):
        calls["n"] += 1
        return False   # would say DOWN — but the Redis hit must win, proving no probe

    monkeypatch.setattr(hp, "_client_health", counting)
    assert await hp._cached_health("glm_5_2") is True
    assert calls["n"] == 0                          # served from the shared cache
    assert hp._health_cache["glm_5_2"][1] is True   # local fallback refreshed


@pytest.mark.asyncio
async def test_cached_health_redis_miss_probes_and_publishes_with_ttl(monkeypatch):
    """Redis miss ⇒ probe, then publish to Redis with the SAME short TTL (as int ex)."""
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_TTL", "30")
    fake = _FakeRedis()
    _wire_fake_redis(monkeypatch, fake)
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(True))

    assert await hp._cached_health("deepseek_v4_flash") is True
    assert fake.store[hp._health_key("deepseek_v4_flash")] == "1"
    assert fake.set_calls and fake.set_calls[-1][2] == 30   # ex == int(ttl)


@pytest.mark.asyncio
async def test_cached_health_ignores_shared_unhealthy(monkeypatch):
    """F7 share-only-healthy: a stray '0' in Redis must NOT poison the fleet — it is
    ignored and each worker re-probes locally (owning its own negative), so a backend that
    has since recovered is not blinded for a TTL by one worker's earlier blip."""
    hp.reset_health_cache()
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    fake = _FakeRedis(seed={hp._health_key("kimi_code"): "0"})
    _wire_fake_redis(monkeypatch, fake)
    calls = {"n": 0}

    async def counting(_b):
        calls["n"] += 1
        return True   # the backend is actually UP; the stale '0' must not win

    monkeypatch.setattr(hp, "_client_health", counting)
    assert await hp._cached_health("kimi_code") is True
    assert calls["n"] == 1                               # re-probed, not poisoned


@pytest.mark.asyncio
async def test_cached_health_unhealthy_probe_not_published(monkeypatch):
    """F7: an unhealthy probe is NEVER written to Redis (only healthy is shared), so one
    worker's miss can't blind siblings."""
    hp.reset_health_cache()
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    fake = _FakeRedis()
    _wire_fake_redis(monkeypatch, fake)
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(False))
    assert await hp._cached_health("glm_5_2") is False   # local negative owns it
    assert fake.set_calls == []                          # nothing published
    assert hp._health_key("glm_5_2") not in fake.store


@pytest.mark.asyncio
async def test_cached_health_redis_fail_open_to_local_probe(monkeypatch):
    """Redis down (get/set raise) ⇒ fall back to the local probe; no crash."""
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    fake = _FakeRedis(fail=True)
    _wire_fake_redis(monkeypatch, fake)
    calls = {"n": 0}

    async def counting(_b):
        calls["n"] += 1
        return True

    monkeypatch.setattr(hp, "_client_health", counting)
    assert await hp._cached_health("glm_5_2") is True   # local probe used
    assert calls["n"] == 1
    assert hp._health_cache["glm_5_2"][1] is True       # local cache still written


# ── live_health_probe: throttle + cache + fail-open composed ─────────────────────

@pytest.mark.asyncio
async def test_live_probe_healthy_when_reachable_and_unpinned(monkeypatch):
    monkeypatch.setattr(hp, "_is_throttled", lambda b: _async(False))
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(True))
    assert await hp.live_health_probe("deepseek_v4_flash") is True


@pytest.mark.asyncio
async def test_live_probe_unavailable_when_throttled_skips_health(monkeypatch):
    health_called = {"n": 0}

    async def health(_b):
        health_called["n"] += 1
        return True

    monkeypatch.setattr(hp, "_is_throttled", lambda b: _async(True))
    monkeypatch.setattr(hp, "_client_health", health)
    assert await hp.live_health_probe("kimi_code") is False
    assert health_called["n"] == 0   # throttled short-circuits — no health_check


@pytest.mark.asyncio
async def test_live_probe_unavailable_when_unreachable(monkeypatch):
    monkeypatch.setattr(hp, "_is_throttled", lambda b: _async(False))
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(False))
    assert await hp.live_health_probe("claude_api") is False


# ── install + the routing-view live flag ────────────────────────────────────────

def test_install_live_probe_flips_flag_and_wires_resolve():
    assert teams.health_probe_is_live() is False
    hp.install_live_probe()
    assert teams.health_probe_is_live() is True
    assert teams._health_probe is hp.live_health_probe
    # restore (the autouse fixture also restores, but assert the no-op path is reachable)
    teams.set_health_probe(teams._default_health_probe)
    assert teams.health_probe_is_live() is False


# ── JOAT v1 ACCEPTANCE: resolve() walks the chain via the REAL live probe ───────

@pytest.mark.asyncio
async def test_resolve_walks_past_throttled_primary_via_live_probe(monkeypatch):
    """cloud-heavy worker chain = [glm_5_2, deepseek_v4_flash, kimi_code]. Pin
    (throttle) the primary glm_5_2 → resolve, wired to the LIVE probe, must walk to
    the first healthy hop. End-to-end teams.resolve ↔ health_probe."""
    import core.nodes.execute as ex

    async def pin(b):
        return "x" if b == "glm_5_2" else None     # only glm_5_2 throttled

    monkeypatch.setattr(ex, "_check_escalation_pin", pin)
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(True))  # all reachable
    hp.install_live_probe()

    face = _face(preferred_backend="local", role="worker")
    teams.set_active_team("cloud-heavy")
    assert await teams.resolve("worker", face=face) == "deepseek_v4_flash"


@pytest.mark.asyncio
async def test_resolve_returns_primary_when_whole_chain_unhealthy_via_live_probe(monkeypatch):
    import core.nodes.execute as ex

    monkeypatch.setattr(ex, "_check_escalation_pin", lambda b: _async(None))
    monkeypatch.setattr(hp, "_client_health", lambda b: _async(False))  # nothing reachable
    hp.install_live_probe()

    face = _face(preferred_backend="local", role="worker")
    teams.set_active_team("cloud-heavy")
    # whole chain down ⇒ primary (execute_node's runtime fallback then handles it).
    assert await teams.resolve("worker", face=face) == "glm_5_2"


@pytest.mark.asyncio
async def test_default_team_never_probes_even_with_live_probe(monkeypatch):
    """The no-regression guard: with NO active team, resolve passthrough returns the
    face's preferred_backend WITHOUT ever calling the probe (no network even live)."""
    probed = {"n": 0}

    async def probe(_b):
        probed["n"] += 1
        return True

    teams.set_health_probe(probe)
    face = _face(preferred_backend="kimi_code", role="worker")
    assert await teams.resolve("worker", face=face) == "kimi_code"
    assert probed["n"] == 0


def _async(value):
    """A coroutine that immediately returns *value* (for one-line monkeypatches)."""
    async def _coro(*_a, **_k):
        return value
    return _coro()
