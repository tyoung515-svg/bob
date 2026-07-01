"""BoBClaw Core — tests for the JOAT team-builder tools (list_backends, create_team)."""
from __future__ import annotations

import json

import pytest

from core import teams
from core.tools.registry import NATIVE_TOOLS, get_tools
from core.tools.teams_tool import create_team, list_backends


@pytest.fixture
def teams_dir(tmp_path):
    teams.set_custom_teams_dir(tmp_path)
    yield tmp_path
    teams.set_custom_teams_dir(None)


def test_list_backends_returns_palette_and_roles():
    payload = json.loads(list_backends.invoke({}))
    backends = {b["backend"] for b in payload["backends"]}
    assert "deepseek_v4_flash" in backends and "local" in backends
    one = next(b for b in payload["backends"] if b["backend"] == "deepseek_v4_flash")
    assert "max_usd_per_worker" in one and "max_fanout_width" in one
    assert payload["roles"] == ["apex", "worker", "critic"]


def test_create_team_tool_persists(teams_dir):
    out = create_team.invoke({
        "name": "tool-fleet",
        "roles": {"worker": {"backend": "deepseek_v4_flash", "escalation_chain": ["glm_5_2"]}},
    })
    assert "Created team 'tool-fleet'" in out
    assert "tool-fleet" in teams.known_teams()


def test_create_team_tool_returns_error_on_bad_backend(teams_dir):
    out = create_team.invoke({"name": "bad", "roles": {"apex": {"backend": "no-such-backend"}}})
    assert out.startswith("Error:")
    assert "bad" not in teams.known_teams()


def test_team_tools_registered_and_gateable():
    assert "create_team" in NATIVE_TOOLS and "list_backends" in NATIVE_TOOLS
    got = {t.name for t in get_tools(["list_backends", "create_team"])}
    assert got == {"list_backends", "create_team"}
