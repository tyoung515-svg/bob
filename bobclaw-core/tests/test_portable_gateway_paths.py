"""Check gateway authorization against default and relocated lane roots."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def run_gateway(code, tmp_path, overrides):
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ("BOB_FAMILY_ROOT", "BOB_SPS_LANE_ROOT", "SPS_LANE_ROOT"):
        env.pop(name, None)
    env.update(overrides)
    env["PYTHONPATH"] = os.pathsep.join(str(repo / p) for p in ("bobclaw-core", "bobclaw-gateway", "bobclaw-discord"))
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_agent_memory_scopes_use_storage_root(tmp_path):
    overrides = {"BOB_FAMILY_ROOT": str(tmp_path / "family")}
    run_gateway('''
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from core.paths import family_root
from routers import agent_memory

root = family_root()
app = web.Application()
agent_memory.setup(app)
assert app[agent_memory.ROOT] == root
request = make_mocked_request("GET", "/agent-memory/status", app=app)
request["user"] = {"token_type": "agent", "scope": {"may_touch": [str(root / "dad/**")]}}
assert agent_memory.allowed(request, "dad")
assert not agent_memory.allowed(request, "luna")
assert not agent_memory.allowed_path(request, str(root / "luna/MEMORY.md"))
request["user"]["scope"]["may_not_touch"] = [str(root / "dad/**")]
assert not agent_memory.allowed(request, "dad")
request["user"]["scope"] = {"may_touch": ["/var/lib/other/family/dad/**"]}
assert not agent_memory.allowed(request, "dad")
''', tmp_path, overrides)


@pytest.mark.parametrize("sps_var", ["BOB_SPS_LANE_ROOT", "SPS_LANE_ROOT"])
def test_custom_paths_wired_through_all_services(tmp_path, sps_var):
    """A fresh interpreter exercises import-time configuration without leaking it."""
    overrides = {
        "BOB_FAMILY_ROOT": str(tmp_path / "family"),
        "BOB_DEFAULT_OWNER": "dad",
        sps_var: str(tmp_path / "sps"),
        "BOB_SHARED_LANE_ROOT": str(tmp_path / "board"),
        "MEMORY_SQLITE_PATH": str(tmp_path / "memory.db"),
    }
    run_gateway('''
from pathlib import Path
from core import paths
from core.config import config as core_config
from core.memory.bootstrap import MemoryBootstrapConfig
from bobclaw_discord.board import DEFAULT_LANE_ROOT as board_root
from bobclaw_discord.config import DEFAULT_LANE_ROOT as config_root
from routers import agent_memory, work_memory
from auth import lane_from_scope
from gateway import build_app, config
from routers import agent_services
from unittest.mock import patch

root = paths.family_root()
work_scope = {"may_touch": [str(paths.sps_lane_root() / "**")], "may_not_touch": [str(root / "**")]}
dad = {"user": {"token_type": "agent", "scope": {"may_touch": [str(root / "dad/**")]}}}
work = {"user": {"token_type": "agent", "scope": work_scope}}
assert work_memory.ROOT == paths.sps_lane_root()
assert lane_from_scope(work_scope) == "sps"
assert work_memory.can_write(work)
assert work_memory.can_read(dad) and not work_memory.can_write(dad)
assert not work_memory.can_read({"user": {"scope": {"may_touch": [str(root / "luna/**")]}}})
assert board_root == paths.shared_lane_root() == Path(config_root)
assert Path(core_config.MEMORY_SQLITE_PATH) == paths.memory_sqlite_path()
assert MemoryBootstrapConfig.from_env(core_config).sqlite_path == paths.memory_sqlite_path()
config.CORE_URL = "http://127.0.0.1:7835"
original_setup = agent_services.setup
with patch.object(agent_services, "setup", lambda app: original_setup(app, root=paths.memory_sqlite_path().parent / "agent-services")):
    app = build_app()
assert app[agent_memory.ROOT] == root
assert app[agent_memory.DB] == paths.memory_sqlite_path()
''', tmp_path, overrides)


def test_relocated_family_sources_and_save_stay_in_owner_lane(tmp_path):
    run_gateway('''
import asyncio
import json
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from core.paths import family_root
from routers import agent_memory as m

root = family_root()
for owner in ("dad", "luna"):
    (root / owner).mkdir(parents=True)
    (root / owner / "MEMORY.md").write_text(owner + " original")
app = web.Application()
m.setup(app)
request = make_mocked_request("POST", "/agent-memory/save", app=app)
request["user"] = {"token_type": "agent", "scope": {"may_touch": [str(root / "dad/**")]}}
assert {row["owner"] for row in m.sources(request)} == {"dad"}
with patch.object(m, "body", AsyncMock(return_value={"content": "portable saved marker"})):
    response = asyncio.run(m.save(request))
assert response.status == 201
assert json.loads(response.text)["owner"] == "dad"
assert "portable saved marker" in (root / "dad/MEMORY.md").read_text()
assert (root / "luna/MEMORY.md").read_text() == "luna original"
request["user"]["scope"] = {"may_touch": [str(root / "luna/**")]}
assert {row["owner"] for row in m.sources(request)} == {"luna"}
assert not any("portable saved marker" in row["content"] for row in m.sources(request))
''', tmp_path, {"BOB_FAMILY_ROOT": str(tmp_path / "family")})
