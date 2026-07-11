"""Boot regression guard for ``start._on_startup``.

BoB's production server lifecycle (``start._on_startup``) is NOT exercised by the
rest of the unit suite, so a bug that only manifests at boot can pass CI while
the real server dies on startup ("green tests, dead server"). The concrete
failure this guards against: ``BUILD_SANDBOX`` / ``BUILD_SANDBOX_IMAGE`` are
module-level globals defined BELOW ``config = BoBClawConfig()`` in
``core/config.py``, so reading them off the ``config`` INSTANCE
(``config.BUILD_SANDBOX``) raises ``AttributeError``. ``_on_startup`` must read
them via the module (``import core.config as _config``).

This runs ``_on_startup`` with its I/O (SQLite, Postgres, graph, health probe,
docker probe) mocked, so the config-access + control flow is actually executed
under the socket-less test harness.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

import core.config as _config
import start
from core.ledger.federation import FederationRegistry


async def test_on_startup_reads_build_sandbox_without_attributeerror(monkeypatch):
    # Mock the I/O dependencies so only the pure config-access path runs.
    monkeypatch.setattr(start, "init_sqlite", AsyncMock())
    monkeypatch.setattr(start, "init_postgres", AsyncMock(return_value=MagicMock(name="pool")))
    monkeypatch.setattr(start, "create_graph", AsyncMock(return_value=MagicMock(name="graph")))
    monkeypatch.setattr("core.health_probe.install_live_probe", lambda: None)
    # BUILD_SANDBOX defaults to "docker"; force docker_ready False so _on_startup
    # takes the branch that also reads BUILD_SANDBOX_IMAGE — exercising BOTH of
    # the previously-crashing reads, not just the first.
    monkeypatch.setattr("core.build.sandbox.docker_ready", lambda: False)
    # Memory OFF so we don't touch the qdrant/embedder bootstrap path.
    monkeypatch.setattr(start.config, "MEMORY_ENABLED", False, raising=False)
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "0")

    app = web.Application()
    await start._on_startup(app)  # must NOT raise AttributeError

    # The settings _on_startup reads live on the MODULE, not the config instance.
    assert hasattr(_config, "BUILD_SANDBOX")
    assert hasattr(_config, "BUILD_SANDBOX_IMAGE")
    # And the hook wired up the pool + graph it built.
    assert app[start.POOL_KEY] is not None
    assert app[start.GRAPH_KEY] is not None


async def test_on_startup_memory_off_keeps_registry_unloaded(monkeypatch):
    """MEMORY_ENABLED=false keeps the legacy boot path free of fence/registry work."""
    monkeypatch.setattr(start, "init_sqlite", AsyncMock())
    monkeypatch.setattr(
        start,
        "init_postgres",
        AsyncMock(return_value=MagicMock(name="pool")),
    )
    monkeypatch.setattr(
        start,
        "create_graph",
        AsyncMock(return_value=MagicMock(name="graph")),
    )
    monkeypatch.setattr("core.health_probe.install_live_probe", lambda: None)
    monkeypatch.setattr("core.build.sandbox.docker_ready", lambda: False)
    monkeypatch.setattr(start.config, "MEMORY_ENABLED", False, raising=False)
    monkeypatch.setenv("BOBCLAW_HEALTH_PROBE_REDIS", "0")

    def fail_registry_load(self):
        raise AssertionError("memory-off startup must not load the federation registry")

    monkeypatch.setattr(FederationRegistry, "load", fail_registry_load)
    await start._on_startup(web.Application())


async def test_on_cleanup_releases_memory_fence_and_postgres(monkeypatch):
    """Shutdown deterministically releases the held family lock via singletons."""
    close_order = []
    fence = MagicMock(name="write_fence")
    fence.close.side_effect = lambda: close_order.append("fence")
    provider = MagicMock(name="memory_provider")
    provider.close.side_effect = lambda: close_order.append("provider")
    pool = MagicMock(name="pool")
    pool.close = AsyncMock()
    monkeypatch.setattr(
        "core.memory.bootstrap.get_memory",
        lambda: SimpleNamespace(
            write_fence=fence,
            indexer=SimpleNamespace(_provider=provider),
        ),
    )
    app = web.Application()
    app[start.POOL_KEY] = pool

    await start._on_cleanup(app)

    provider.close.assert_called_once_with()
    fence.close.assert_called_once_with()
    assert close_order == ["provider", "fence"]
    pool.close.assert_awaited_once_with()
