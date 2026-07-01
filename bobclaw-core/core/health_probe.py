"""BoBClaw Core — JOAT v1 LIVE health-walk probe.

This is the v1 fill for the no-op seam shipped in JOAT v0 (``core/teams.py``
``_default_health_probe`` always returned True). It answers ONE question for the
team resolver — *is this backend usable right now?* — so ``teams.resolve`` walks
the escalation chain around an unhealthy or throttled backend instead of routing
into a hole.

WHY this lives OUTSIDE ``teams.py``: ``teams.py`` is DATA-only (backend strings,
no model names, no backend-client / Redis imports — guarded by
``test_no_model_names_in_core`` and the "core stays import-light" convention). The
live probe necessarily touches the backend clients + the Redis throttle pins, so it
lives here and is INJECTED into the resolver via ``teams.set_health_probe`` at
process startup (``start.py._on_startup``). Tests and the default (server-less)
import path keep the no-op probe, so the JOAT v0 passthrough contract is byte-for-
byte preserved unless the live probe is explicitly installed.

Two availability signals, deliberately split by how time-critical / shared each is:

  * **Throttle pin** (``execute._check_escalation_pin``) — the Redis-backed 429
    signal that ``execute_node`` writes when a backend rate-limits. Already shared
    across every core worker, and time-critical (a fresh pin must be honored at
    once), so it is read **LIVE / uncached** every probe. A pinned backend ⇒
    unavailable ⇒ ``resolve`` walks past it.
  * **Reachability** (each backend client's ``health_check()``) — the slow-moving
    up/down signal. ``health_check`` can hit the network (HTTP ``GET /models``, ~5s)
    or spawn a subprocess (``<cli> --version``), so it is wrapped in a short-TTL,
    **process-local** cache: ``resolve`` never network-probes every turn, and each
    worker re-probes a given backend at most once per TTL. (Process-local, not
    Redis-shared, on purpose: the dynamic 429 signal is already Redis-shared via the
    pins; sharing the slow reachability signal too would add a Redis write path /
    failure surface for little gain. Redis-caching reachability is a possible v1.1.)

FAIL-OPEN throughout: a raising / timing-out / unknown probe ⇒ *available*, matching
the v0 default and the ``teams._is_available`` outer guard — a flaky health check
must never strand a backend the team explicitly declared.

The backend-string → client dispatch below MIRRORS
``execute._default_send_to_backend``; ``test_health_probe.py`` asserts every
``KNOWN_BACKENDS`` string is covered so the two can't silently drift.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from core.config import KNOWN_BACKENDS

logger = logging.getLogger(__name__)


# ── Build-pipe-verified pure primitives (harvested) ─────────────────────────────
# These three deterministic decisions were authored + Docker-verified through the
# build pipeline (tasks/2026-06-28-joat-v1-health-walk/) and harvested here, the
# same way P3 harvested the verified scope-vouch helpers. Pure, total, no I/O.

def is_backend_available(throttled: bool, healthy: bool) -> bool:
    """Combine the two signals: an active throttle pin routes AROUND a backend
    (unavailable) regardless of reachability; otherwise availability == reachability."""
    if throttled:
        return False
    return healthy


def cache_is_fresh(expiry: float, now: float) -> bool:
    """A TTL cache entry is fresh only strictly before its expiry instant."""
    return expiry > now


def walk_to_available(candidates: list[str], availability: dict[str, bool]) -> str:
    """The escalation-chain walk, as a pure function (mirrors ``teams.resolve``'s
    inline walk; kept here as the documented, build-pipe-verified contract that
    ``test_teams.py`` asserts ``resolve`` obeys). First candidate whose availability
    is truthy wins; a candidate MISSING from the map is treated available (fail-open);
    whole chain explicitly down ⇒ the primary (``candidates[0]``)."""
    if not candidates:
        raise ValueError("candidates must be non-empty")
    for candidate in candidates:
        if availability.get(candidate, True):
            return candidate
    return candidates[0]


# ── Reachability dispatch — MIRRORS execute._default_send_to_backend ─────────────
# Backends whose reachability is a real client ``health_check()``. (HTTP clients
# short-circuit False on a missing key with NO network — see kimi.py.) ``local`` and
# ``opencode_serve`` have no single-client health and are handled specially below.
_HTTP_CLIENT_BACKENDS: frozenset[str] = frozenset({
    "kimi_code",
    "kimi_platform",
    "claude_api",
    "deepseek_v4_flash",
    "glm_5_2",
    "minimax",
    "gemini_flash",
    "gemini_pro",
    "gemini_deep_research",
    "claude_code",   # subprocess `claude --version`
    "agy_code",      # subprocess `agy --version`
    "codex_code",    # subprocess `codex --version` + LiteLLM proxy liveness
    "kimi_cli",      # subprocess `kimi --version`
})
_SPECIAL_BACKENDS: frozenset[str] = frozenset({"local", "opencode_serve"})

#: Every backend string the probe handles EXPLICITLY. The coverage test asserts
#: ``KNOWN_BACKENDS <= COVERED_BACKENDS`` so adding a backend to the dispatch map
#: without teaching the probe fails loudly.
COVERED_BACKENDS: frozenset[str] = _HTTP_CLIENT_BACKENDS | _SPECIAL_BACKENDS


def _make_client(backend: str):
    """Construct the health-bearing client for an ``_HTTP_CLIENT_BACKENDS`` member.

    Lazy per-backend imports (no import-time cost / cycle into the backend layer when
    the probe module is loaded). Mirrors the dispatch in
    ``execute._default_send_to_backend`` — keep the two in sync (the coverage test guards it).
    """
    if backend == "kimi_code":
        from core.backends.kimi import KimiClient
        return KimiClient()
    if backend == "kimi_platform":
        from core.backends.kimi_platform import KimiPlatformClient
        return KimiPlatformClient()
    if backend == "claude_api":
        from core.backends.claude import ClaudeClient
        return ClaudeClient()
    if backend == "deepseek_v4_flash":
        from core.backends.deepseek import DeepSeekClient
        return DeepSeekClient()
    if backend == "glm_5_2":
        from core.backends.glm import GLMClient
        return GLMClient()
    if backend == "minimax":
        from core.backends.minimax import MiniMaxClient
        return MiniMaxClient()
    if backend in ("gemini_flash", "gemini_pro", "gemini_deep_research"):
        from core.backends.gemini import GeminiClient
        return GeminiClient()
    if backend == "claude_code":
        from core.backends.claude_code import ClaudeCodeClient
        return ClaudeCodeClient()
    if backend == "agy_code":
        from core.backends.agy_code import AntigravityClient
        return AntigravityClient()
    if backend == "codex_code":
        from core.backends.codex_code import CodexCodeClient
        return CodexCodeClient()
    if backend == "kimi_cli":
        from core.backends.kimi_cli import KimiCliClient
        return KimiCliClient()
    raise KeyError(backend)  # pragma: no cover - guarded by COVERED_BACKENDS


async def _client_health(backend: str) -> bool:
    """Reachability for one backend (uncached). MIRRORS the dispatch in
    ``execute._default_send_to_backend``. Unknown backend ⇒ True (fail-open)."""
    if backend in _HTTP_CLIENT_BACKENDS:
        return bool(await _make_client(backend).health_check())
    if backend == "local":
        # `local` is the in-process router lane (no single client). Available iff the
        # router discovers a usable local backend — mirrors the local branch of
        # _default_send_to_backend. Reuse execute._router so a monkeypatched router
        # (tests) and the live discovery cache are both honored.
        from core.nodes.execute import _router
        discovered = await _router.discover()
        return bool(_router.get_best_backend(discovered))
    if backend == "opencode_serve":
        # Workspace-scoped pool; no single endpoint to probe. Fail-open (it is never
        # in a BUILTIN team chain, and a dead serve degrades inside the pool).
        return True
    return True  # unknown backend string ⇒ fail-open (never strand resolve on a typo)


# ── Throttle-pin gate (LIVE / uncached — the shared, time-critical signal) ──────
async def _is_throttled(backend: str) -> bool:
    """True iff a Redis escalation pin is currently routing *backend* around (a recent
    429). Read live every probe so a fresh pin from any core worker is honored at once.
    Redis failure / unknown ⇒ not throttled (fail-open to the reachability check)."""
    try:
        from core.nodes.execute import _check_escalation_pin
        return bool(await _check_escalation_pin(backend))
    except Exception:  # pragma: no cover - defensive; redis already swallows its own
        logger.debug("throttle-pin check failed for %r; treating as un-throttled", backend, exc_info=True)
        return False


# ── Reachability cache (short-TTL; process-local + optional Redis share) ─────────
# Two tiers, both short-TTL:
#   1. process-local (always on) — the fast path AND the fail-open fallback when Redis
#      is down. This is the JOAT-v1 baseline behaviour.
#   2. Redis-shared (A3, OPT-IN via BOBCLAW_HEALTH_PROBE_REDIS) — so a fleet of
#      bobclaw-core workers don't each independently network/subprocess-probe the same
#      backend within a TTL window; one worker's fresh reachability is reused by its
#      siblings, mirroring the Redis-shared throttle pins (execute._pin_escalation).
# Redis-sharing is DEFAULT-OFF so the unit suite (which monkeypatches _client_health and
# asserts probe-call counts) stays offline + byte-identical; production turns it on in
# start.py._on_startup. Fail-OPEN throughout: any Redis hiccup degrades to tier 1.
_health_cache: dict[str, tuple[float, bool]] = {}  # backend -> (expiry_monotonic, healthy)

_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_PROBE_TIMEOUT = 6.0


def _ttl_seconds() -> float:
    try:
        return float(os.getenv("BOBCLAW_HEALTH_PROBE_TTL", "") or _DEFAULT_TTL_SECONDS)
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _probe_timeout() -> float:
    try:
        return float(os.getenv("BOBCLAW_HEALTH_PROBE_TIMEOUT", "") or _DEFAULT_PROBE_TIMEOUT)
    except ValueError:
        return _DEFAULT_PROBE_TIMEOUT


def _redis_share_on() -> bool:
    """Whether reachability is Redis-shared across workers. DEFAULT-OFF (env opt-in):
    start.py._on_startup sets BOBCLAW_HEALTH_PROBE_REDIS=1 for the live server; the test
    / server-less path leaves it unset ⇒ process-local only (byte-identical, offline)."""
    return (os.getenv("BOBCLAW_HEALTH_PROBE_REDIS", "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _health_key(backend: str) -> str:
    return f"bobclaw:health:{backend}"


async def _redis_get_health(backend: str) -> Optional[bool]:
    """Shared reachability from Redis, or None when absent / Redis unavailable (caller
    then re-probes). Reuses execute's Redis client + the throttle-pin's fail-open policy."""
    try:
        from core.nodes.execute import _get_redis
        value = await _get_redis().get(_health_key(backend))
    except Exception:  # fail-open: degrade to a local probe
        logger.debug("Redis health read failed for %r; using process-local", backend, exc_info=True)
        return None
    # SHARE-ONLY-HEALTHY (F7): only a "1" is ever a valid shared signal. Any other value
    # (absent, or a stray legacy "0") returns None so the caller re-probes locally — the
    # negative is NEVER shared, so one worker's blip can't blind the fleet for a TTL.
    return True if value == "1" else None


async def _redis_set_health(backend: str, healthy: bool) -> None:
    """Publish a freshly-probed reachability to Redis with the SAME short TTL as the local
    cache, so sibling workers skip the probe. Best-effort (swallow Redis errors).

    SHARE-ONLY-HEALTHY (F7): publishes ONLY a healthy ("1") result. An unhealthy probe is
    NEVER written — otherwise one worker hitting a transient error would poison every
    sibling (they'd read "0" and skip a backend that has since recovered) for up to the
    TTL. The negative stays process-local: each worker re-probes and owns its own miss; a
    recovered backend's stale "1" simply ages out and the next probe republishes."""
    if not healthy:
        return
    try:
        from core.nodes.execute import _get_redis
        ttl = max(1, int(round(_ttl_seconds())))  # Redis `ex` must be a positive int
        await _get_redis().set(_health_key(backend), "1", ex=ttl)
    except Exception:  # fail-open: the local cache still holds the value
        logger.debug("Redis health write failed for %r; process-local only", backend, exc_info=True)


def reset_health_cache() -> None:
    """Drop the process-local reachability cache (test isolation / forced re-probe).

    Does NOT clear the Redis-shared tier (it is TTL-bounded and shared across workers —
    one worker resetting must not blind the fleet); the local clear is enough to force a
    re-probe in-process, and the short TTL ages the shared entry out."""
    _health_cache.clear()


async def _cached_health(backend: str) -> bool:
    """Reachability with a short-TTL cache (process-local, + Redis-shared when opted in)
    and a hard timeout. Fail-open: a raising or hung ``health_check`` ⇒ available (cached
    briefly so a persistent failure doesn't tight-loop the probe).

    Order: local cache → Redis-shared cache (if on) → live probe (then publish to both).
    """
    now = time.monotonic()
    hit = _health_cache.get(backend)
    if hit is not None and cache_is_fresh(hit[0], now):
        return hit[1]
    # Tier 2: a sibling worker's recent probe (opt-in). Refresh the local fallback so a
    # subsequent in-process call skips even the Redis round-trip.
    if _redis_share_on():
        shared = await _redis_get_health(backend)
        if shared is not None:
            _health_cache[backend] = (now + _ttl_seconds(), shared)
            return shared
    try:
        healthy = await asyncio.wait_for(_client_health(backend), timeout=_probe_timeout())
    except asyncio.TimeoutError:
        logger.debug("health_check for %r timed out; assuming available", backend)
        healthy = True
    except Exception:
        logger.debug("health_check for %r raised; assuming available", backend, exc_info=True)
        healthy = True
    _health_cache[backend] = (now + _ttl_seconds(), healthy)
    if _redis_share_on():
        await _redis_set_health(backend, healthy)
    return healthy


async def live_health_probe(backend: str) -> bool:
    """The JOAT v1 probe installed into ``teams._health_probe`` at startup.

    A backend is available iff it is NOT throttle-pinned AND its (cached) reachability
    check passes. Fail-open everywhere (see module docstring). NOTE: ``teams.resolve``
    only calls this when an ACTIVE team is selected — the DEFAULT (per-face passthrough)
    never probes, so the JOAT v0 no-regression contract holds untouched.
    """
    throttled = await _is_throttled(backend)
    if throttled:
        return is_backend_available(throttled=True, healthy=False)
    healthy = await _cached_health(backend)
    return is_backend_available(throttled=False, healthy=healthy)


def install_live_probe(*, reset_cache: bool = True) -> None:
    """Wire the live probe into the team resolver. Called once from
    ``start.py._on_startup`` (the production server lifecycle) — NOT at import time, so
    the test / server-less path keeps the no-op default and JOAT v0 passthrough."""
    from core import teams

    if reset_cache:
        reset_health_cache()
    teams.set_health_probe(live_health_probe)
    logger.info("JOAT v1: live health-walk probe installed into teams.resolve")
