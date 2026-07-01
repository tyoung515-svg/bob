"""
BoBClaw Core — OpenCode serve instance pool + dispatch

Registry shape (env-derived, consistent across processes):
  Worker hosts/ports/workspace_dirs come from OPENCODE_INSTANCES via
  config.opencode_instances_parsed(). Every core process reads the
  same env var and converges on the same worker list — no Redis or
  cross-process coordination needed.

Per-process runtime state (intentional, not a sharing bug):
  _Instance.alive — set by this process's health probe loop. Different
    core processes may have different network paths to workers; we
    deliberately keep this view local rather than picking one truth.
  _Instance.in_flight — incremented/decremented per-process around
    each dispatch. Load balancing is approximately, not globally,
    optimal; the imprecision is bounded and acceptable today.

Lifecycle: callers that construct their own pool (not the module-level
singleton) should await pool.close() during shutdown to cancel the
background health-probe task. The module-level _pool singleton's close
is wired into bobclaw-gateway/_on_cleanup (Sprint 6-4A); the gateway
leaks across hot reloads otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from core.backends.opencode_serve import OpenCodeServeClient
from core.config import config


def _normalize_workspace(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


# Rows older than this threshold are treated as alive=False regardless of
# stored value, since no process is currently probing them.
_HEALTH_STALENESS_FACTOR = 2  # multiplier on OPENCODE_HEALTH_PROBE_INTERVAL_S


class NoOpenCodeAvailable(Exception):
    """Raised when no OpenCode instance is available for the requested workspace."""


@dataclass
class _Instance:
    client: OpenCodeServeClient
    workspace_dir: str
    alive: bool = True       # per-process fallback view; Postgres is authoritative when available
    in_flight: int = 0       # per-process — see module docstring
    host: str = ""           # tracked here because OpenCodeServeClient does not expose it
    port: int = 0


class OpenCodeServePool:
    """Env-driven worker registry with per-process health/load tracking. See module docstring for the topology rationale."""

    def __init__(self) -> None:
        self._instances: list[_Instance] = []
        self._probe_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._load_instances()

    def _load_instances(self) -> None:
        for host, port, workspace_dir in config.opencode_instances_parsed():
            client = OpenCodeServeClient(host, port)
            self._instances.append(_Instance(
                client=client,
                workspace_dir=_normalize_workspace(workspace_dir),
                host=host,
                port=port,
            ))

    async def _probe_loop(self) -> None:
        while not self._shutdown:
            await self._probe_all()
            await asyncio.sleep(config.OPENCODE_HEALTH_PROBE_INTERVAL_S)

    async def _probe_all(self) -> None:
        for inst in self._instances:
            try:
                inst.alive = await inst.client.health_check()
            except Exception:
                inst.alive = False
            await self._write_health(
                host=inst.host,
                port=inst.port,
                alive=inst.alive,
            )

    async def _write_health(self, host: str, port: int, alive: bool) -> None:
        """UPSERT this process's view of (host, port) health. No-op on Postgres failure."""
        try:
            from core.db import get_pool
            pool = get_pool()
        except Exception as exc:
            logger.debug("Postgres pool unavailable for health write: %s", exc)
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO opencode_instance_health (host, port, alive, last_probe_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (host, port) DO UPDATE SET
                        alive = EXCLUDED.alive,
                        last_probe_at = EXCLUDED.last_probe_at
                    """,
                    host, port, alive,
                )
        except Exception as exc:
            logger.warning(
                "Postgres health write failed for %s:%d (alive=%s): %s",
                host, port, alive, exc,
            )

    async def _read_shared_alive(self) -> dict[tuple[str, int], bool] | None:
        """Return {(host, port): alive} from Postgres, or None on failure.

        A row is treated as alive=False if its last_probe_at is older than
        2× the probe interval (no process is currently watching it).
        """
        try:
            from core.db import get_pool
            pool = get_pool()
        except Exception as exc:
            logger.debug("Postgres pool unavailable for health read: %s", exc)
            return None
        try:
            staleness_seconds = (
                _HEALTH_STALENESS_FACTOR * config.OPENCODE_HEALTH_PROBE_INTERVAL_S
            )
            rows = await pool.fetch(
                """
                SELECT host, port, alive, last_probe_at
                FROM opencode_instance_health
                """,
            )
        except Exception as exc:
            logger.warning("Postgres health read failed; falling back to local: %s", exc)
            return None

        now = datetime.now(timezone.utc)
        result: dict[tuple[str, int], bool] = {}
        for row in rows:
            age = (now - row["last_probe_at"]).total_seconds()
            if age > staleness_seconds:
                result[(row["host"], row["port"])] = False
            else:
                result[(row["host"], row["port"])] = bool(row["alive"])
        return result

    def _ensure_probe_task(self) -> None:
        """Lazily start the health-probe loop on first use.

        Safe to call when there's no running event loop — silently no-ops.
        """
        if (
            self._probe_task is None
            and not self._shutdown
            and self._instances
        ):
            try:
                self._probe_task = asyncio.create_task(self._probe_loop())
            except RuntimeError:
                # No running event loop yet; next dispatch will retry.
                pass

    async def dispatch(
        self,
        messages: list[dict],
        workspace_dir: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Pick the least-busy alive instance for *workspace_dir* and run chat().

        If *workspace_dir* is None, any instance is eligible.
        Raises NoOpenCodeAvailable if no suitable instance is alive.
        """
        self._ensure_probe_task()
        norm_ws = _normalize_workspace(workspace_dir) if workspace_dir is not None else None
        shared_alive = await self._read_shared_alive()

        def _is_alive(inst: _Instance) -> bool:
            if shared_alive is None:
                # Postgres unreachable — fall back to per-process view
                return inst.alive
            key = (inst.host, inst.port)
            # Trust shared view when present; treat unknown rows as local view
            return shared_alive.get(key, inst.alive)

        candidates = [
            inst
            for inst in self._instances
            if _is_alive(inst)
            and (norm_ws is None or _normalize_workspace(inst.workspace_dir) == norm_ws)
        ]
        if not candidates:
            raise NoOpenCodeAvailable(
                f"No OpenCode instance available for workspace={workspace_dir!r}"
            )

        best = min(candidates, key=lambda i: i.in_flight)
        best.in_flight += 1
        try:
            return await best.client.chat(
                messages, workspace_dir=norm_ws, **kwargs
            )
        finally:
            best.in_flight -= 1

    async def close(self) -> None:
        """Cancel the health-probe background task and clean up."""
        self._shutdown = True
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
            self._probe_task = None


# Module-level singleton — replace in tests via monkeypatch
_pool = OpenCodeServePool()
