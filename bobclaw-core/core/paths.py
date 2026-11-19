"""Deployment paths shared by core, gateway and Discord.

Read the environment at call time so callers can load their existing env file
first. Relative memory database paths remain relative to bobclaw-core, as in
the memory bootstrap. The new SPS variable takes precedence over its old alias.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "bobclaw-core"
DEFAULT_MEMORY_SQLITE_PATH = CORE_ROOT / ".memory" / "bobclaw_memory.db"


def _configured_path(name: str, alias: str | None = None) -> Path | None:
    raw = os.environ.get(name, os.environ.get(alias) if alias else None)
    return Path(raw) if raw and raw.strip() else None


def family_root() -> Path | None:
    return _configured_path("BOB_FAMILY_ROOT")


def family_memory_path(owner: str | None = None) -> Path | None:
    """Use the configured default owner only when no caller owner is known."""
    if owner is None:
        owner = os.environ.get("BOB_DEFAULT_OWNER")
    root = family_root()
    return root / owner / "MEMORY.md" if root is not None and owner else None


def sps_lane_root() -> Path | None:
    """None means the work lane is unavailable; never select the current directory."""
    return _configured_path("BOB_SPS_LANE_ROOT", "SPS_LANE_ROOT")


def shared_lane_root() -> Path | None:
    return _configured_path("BOB_SHARED_LANE_ROOT")


def memory_sqlite_path() -> Path:
    lane = os.environ.get("BOB_LANE_ROOT")
    default = Path(lane) / 'bobclaw_memory.db' if lane else DEFAULT_MEMORY_SQLITE_PATH
    path = Path(os.environ.get("MEMORY_SQLITE_PATH", str(default)))
    return path if path.is_absolute() else CORE_ROOT / path
