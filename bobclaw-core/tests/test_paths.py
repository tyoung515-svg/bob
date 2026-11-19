"""Portable paths require explicit lane configuration and resolve overrides consistently."""
from pathlib import Path

import pytest

from core import paths
from core.mcp.server import MCPConfig
from core.permissions import Scope, evaluate_path


@pytest.fixture(autouse=True)
def clean_path_env(monkeypatch):
    for name in ("BOB_FAMILY_ROOT", "BOB_DEFAULT_OWNER", "BOB_SPS_LANE_ROOT",
                 "SPS_LANE_ROOT", "BOB_SHARED_LANE_ROOT", "MEMORY_SQLITE_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_unconfigured_lanes_are_off():
    assert paths.family_root() is None
    assert paths.family_memory_path() is None
    assert paths.sps_lane_root() is None
    assert paths.shared_lane_root() is None
    assert not MCPConfig("unused", "http://127.0.0.1:7826", [], scope={"may_touch": ["**"]}).personal_memory_allowed
    repo = Path(__file__).resolve().parents[2]
    assert paths.memory_sqlite_path() == repo / "bobclaw-core/.memory/bobclaw_memory.db"


def test_custom_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("BOB_FAMILY_ROOT", str(tmp_path / "family"))
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "luna")
    monkeypatch.setenv("BOB_SPS_LANE_ROOT", str(tmp_path / "sps"))
    monkeypatch.setenv("BOB_SHARED_LANE_ROOT", str(tmp_path / "board"))
    monkeypatch.setenv("MEMORY_SQLITE_PATH", str(tmp_path / "memory.db"))
    monkeypatch.chdir(tmp_path)
    assert paths.family_root() == tmp_path / "family"
    assert paths.family_memory_path() == tmp_path / "family/luna/MEMORY.md"
    assert paths.family_memory_path("dad") == tmp_path / "family/dad/MEMORY.md"
    assert paths.sps_lane_root() == tmp_path / "sps"
    assert paths.shared_lane_root() == tmp_path / "board"
    assert paths.memory_sqlite_path() == tmp_path / "memory.db"


def test_sps_alias_and_new_name_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("SPS_LANE_ROOT", str(tmp_path / "old"))
    assert paths.sps_lane_root() == tmp_path / "old"
    monkeypatch.setenv("BOB_SPS_LANE_ROOT", str(tmp_path / "new"))
    assert paths.sps_lane_root() == tmp_path / "new"


def test_relative_sqlite_path_is_core_relative(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORY_SQLITE_PATH", "lane/memory.db")
    assert paths.memory_sqlite_path() == paths.CORE_ROOT / "lane/memory.db"


def test_scope_refusal_and_mcp_surface(monkeypatch, tmp_path):
    monkeypatch.setenv("BOB_FAMILY_ROOT", str(tmp_path / "family"))
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "dad")
    root = paths.family_root()
    scope = {"may_touch": [str(root / "dad/**")]}
    validated = Scope.model_validate(scope)
    assert evaluate_path(str(root / "dad/MEMORY.md"), validated) == "auto"
    assert evaluate_path(str(root / "luna/MEMORY.md"), validated) != "auto"
    cfg = MCPConfig("unused", "http://127.0.0.1:7826", [], scope=scope)
    assert cfg.personal_memory_allowed
    scope["may_not_touch"] = [str(root / "dad/**")]
    assert not cfg.personal_memory_allowed
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "luna")
    assert not cfg.personal_memory_allowed
    cfg.scope = {"may_touch": [str(root / "luna/**")]}
    assert cfg.personal_memory_allowed


@pytest.mark.parametrize("name", ["BOB_FAMILY_ROOT", "BOB_SPS_LANE_ROOT", "BOB_SHARED_LANE_ROOT"])
def test_blank_paths_are_off(monkeypatch, name):
    monkeypatch.setenv(name, "  ")
    getter = {"BOB_FAMILY_ROOT": paths.family_root, "BOB_SPS_LANE_ROOT": paths.sps_lane_root,
              "BOB_SHARED_LANE_ROOT": paths.shared_lane_root}[name]
    assert getter() is None
