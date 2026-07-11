"""
BoBClaw Core — entry point

Initialises the SQLite hot-path cache and the Postgres pool on startup,
builds the aiohttp application via :func:`api.server.build_app`, and
runs the HTTP server on ``config.HOST:config.PORT``.

Run directly::

    python start.py

or with overrides::

    python start.py --host 127.0.0.1 --port 7825
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from aiohttp import web

from api.server import GRAPH_KEY, POOL_KEY, build_app
from core.config import config
# BUILD_SANDBOX* (and other trailing settings) are module-level globals defined
# BELOW the `config = BoBClawConfig()` instance in config.py, so they are NOT
# attributes of `config`. Read them via the module — the same access pattern
# core/nodes/build_verify.py and build_plan.py already use.
import core.config as _config
from core.db import init_postgres, init_sqlite
from core.graph import create_graph

logger = logging.getLogger(__name__)


# ─── Lifecycle hooks ──────────────────────────────────────────────────────────

async def _on_startup(app: web.Application) -> None:
    """Open SQLite and Postgres connections before serving traffic."""
    logger.info("bobclaw-core: initialising SQLite cache")
    await init_sqlite()

    logger.info("bobclaw-core: initialising Postgres pool")
    pool = await init_postgres()
    app[POOL_KEY] = pool

    logger.info("bobclaw-core: compiling LangGraph")
    app[GRAPH_KEY] = await create_graph()

    # JOAT v1: wire the LIVE health-walk probe into teams.resolve (per-backend
    # health_check() + Redis throttle pins, cached + fail-open). Done HERE — the
    # production server lifecycle — not at import, so tests / server-less imports keep
    # the no-op default and the JOAT v0 passthrough contract. Only bites under an
    # active team (the default per-face path never probes).
    from core.health_probe import install_live_probe

    # A3 (JOAT v1.1): Redis-share the reachability cache across the multi-process core
    # fleet (one worker's probe is reused by siblings within the TTL). Opt-in env, turned
    # on HERE (the live server) — an operator can force it off with =0. Unit tests never
    # run this hook, so they keep the process-local-only, offline behaviour.
    os.environ.setdefault("BOBCLAW_HEALTH_PROBE_REDIS", "1")
    install_live_probe()

    # Build/verify sandbox posture — surfaced at STARTUP (not just per verify-run) so
    # a deployment that would run LLM-written code un-isolated on the host is loud, not
    # silent. Never raises: chat is unaffected by the sandbox, so we only warn.
    from core.build.sandbox import docker_ready

    _sbx = (_config.BUILD_SANDBOX or "docker").strip().lower()
    if _sbx == "subprocess":
        logger.warning(
            "BUILD SANDBOX: BUILD_SANDBOX=subprocess — the build verify gate will run "
            "LLM-written code UN-ISOLATED on the host. Use ONLY with trusted models."
        )
    elif _sbx == "auto" and not docker_ready():
        logger.warning(
            "BUILD SANDBOX: BUILD_SANDBOX=auto but Docker/image %r is unavailable — a "
            "build turn would run LLM-written code UN-ISOLATED on the host. Build the "
            "image and set BUILD_SANDBOX=docker for real isolation.",
            _config.BUILD_SANDBOX_IMAGE,
        )
    elif _sbx == "docker" and not docker_ready():
        logger.warning(
            "BUILD SANDBOX: BUILD_SANDBOX=docker but Docker/image %r is not ready — "
            "build turns will FAIL CLOSED until Docker is up (chat is unaffected).",
            _config.BUILD_SANDBOX_IMAGE,
        )
    else:
        logger.info("BUILD SANDBOX: mode=%s (isolated).", _sbx)

    if config.MEMORY_ENABLED:
        from core.memory.bootstrap import (
            MemoryBootstrapConfig,
            bootstrap_memory,
        )
        from core.memory.exceptions import MemoryConfigError

        logger.info("bobclaw-core: bootstraping memory subsystem")
        try:
            bootstrap_config = MemoryBootstrapConfig.from_env(config)
            bootstrap_memory(bootstrap_config)
        except MemoryConfigError as exc:
            logger.error("Memory bootstrap failed: %s", exc)
            raise

    logger.info("bobclaw-core: ready")


async def _on_cleanup(app: web.Application) -> None:
    """Release memory's family lock and close Postgres on shutdown."""
    from core.memory.bootstrap import get_memory, reset_memory
    from core.memory.exceptions import MemoryConfigError

    try:
        memory = get_memory()
    except MemoryConfigError:
        pass
    else:
        fence = getattr(memory, "write_fence", None)
        if fence is not None:
            logger.info("bobclaw-core: releasing memory write fence")
            fence.close()
    finally:
        reset_memory()
    pool = app.get(POOL_KEY)
    if pool is not None:
        logger.info("bobclaw-core: closing Postgres pool")
        await pool.close()


# ─── App construction ─────────────────────────────────────────────────────────

def create_app() -> web.Application:
    """Factory used by both ``python start.py`` and gunicorn-style runners."""
    app = build_app()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BoBClaw Core HTTP server")
    parser.add_argument("--host", default=config.HOST, help="Bind host")
    parser.add_argument("--port", type=int, default=config.PORT, help="Bind port")
    parser.add_argument(
        "--log-level", default=config.LOG_LEVEL, help="Python logging level"
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config.validate()
    except ValueError as exc:
        logger.error("config validation failed: %s", exc)
        sys.exit(2)

    app = create_app()
    # Pre-create an event loop so _on_startup hooks run under the same loop
    # that will serve requests — matches aiohttp's default but keeps behaviour
    # explicit if the caller has already configured a policy.
    asyncio.set_event_loop(asyncio.new_event_loop())
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
