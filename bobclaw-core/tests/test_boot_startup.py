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

from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

import core.config as _config
import start


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
