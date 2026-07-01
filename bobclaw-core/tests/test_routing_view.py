"""JOAT v0 P2 — tests for the core ``/api/routing-view`` read endpoint.

Proves the live faces→roles→resolved-backends map renders, the default team is a
passthrough, and selecting a team (via ``?team=`` preview OR the ``BOBCLAW_TEAM``
env) measurably changes the resolved backends — the P2 exit criterion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api.server import build_app
from core import teams
from core.faces.registry import FaceRegistry

PROFILES_DIR = Path(__file__).parent.parent / "core" / "faces" / "profiles"


@pytest.fixture(autouse=True)
def _reset_team_state(tmp_path):
    teams.set_active_team(None)
    teams.set_custom_teams_dir(tmp_path)  # isolate the on-disk team store per test
    yield
    teams.set_active_team(None)
    teams.set_custom_teams_dir(None)


@pytest.fixture
def faces() -> FaceRegistry:
    return FaceRegistry(profiles_dir=PROFILES_DIR)


@pytest.fixture
async def client(faces: FaceRegistry) -> Any:
    app = build_app(faces=faces)
    async with TestClient(TestServer(app)) as c:
        yield c


def _by_id(view: dict) -> dict:
    return {f["id"]: f for f in view["faces"]}


async def test_routing_view_default_team_is_passthrough(client):
    resp = await client.get("/api/routing-view")
    assert resp.status == 200
    view = await resp.json()
    assert view["active_team"] is None
    assert view["teams"] == ["cloud-heavy", "demo-fleet", "hier-fleet", "local-first"]
    assert len(view["faces"]) == 23
    rows = _by_id(view)
    # default team → resolved == preferred for every face
    for f in view["faces"]:
        assert f["resolved_backend"] == f["preferred_backend"], f["id"]
    assert rows["worker-deepseek"]["resolved_backend"] == "deepseek_v4_flash"
    assert rows["worker-deepseek"]["escalation_chain"] == ["kimi_code"]
    assert rows["builder-bob"]["role"] is None  # honestly unset


async def test_routing_view_team_query_remaps_backends(client):
    """?team=cloud-heavy previews the fleet without touching env — workers go to
    glm_5_2, apex (planner-minimax) to claude_code."""
    resp = await client.get("/api/routing-view", params={"team": "cloud-heavy"})
    assert resp.status == 200
    view = await resp.json()
    assert view["active_team"] == "cloud-heavy"
    rows = _by_id(view)
    assert rows["worker-deepseek"]["resolved_backend"] == "glm_5_2"
    assert rows["worker-deepseek"]["escalation_chain"] == ["deepseek_v4_flash", "kimi_code"]
    assert rows["worker-deepseek"]["tool_capable"] is True   # glm_5_2 is tool-capable
    assert rows["planner-minimax"]["resolved_backend"] == "claude_code"
    # builder-bob is roleless → still its own preferred even under a team
    assert rows["builder-bob"]["resolved_backend"] == "local"


async def test_routing_view_demo_fleet_renders_three_tiers(client):
    """The centerpiece view: apex faces→claude_api (Opus), worker faces→
    deepseek_v4_flash, critic (reviewer)→glm_5_2. All three tiers visible."""
    resp = await client.get("/api/routing-view", params={"team": "demo-fleet"})
    assert resp.status == 200
    view = await resp.json()
    assert view["active_team"] == "demo-fleet"
    rows = _by_id(view)
    assert rows["planner-claude"]["resolved_backend"] == "claude_api"   # apex / Opus
    assert rows["worker-deepseek"]["resolved_backend"] == "deepseek_v4_flash"  # worker
    assert rows["reviewer"]["resolved_backend"] == "glm_5_2"            # critic / GLM auditor
    tiers = {rows["planner-claude"]["resolved_backend"],
             rows["worker-deepseek"]["resolved_backend"],
             rows["reviewer"]["resolved_backend"]}
    assert tiers == {"claude_api", "deepseek_v4_flash", "glm_5_2"}


async def test_routing_view_reflects_bobclaw_team_env(client, monkeypatch):
    """The P2 exit: switching BOBCLAW_TEAM and re-hitting shows different backends."""
    monkeypatch.setenv("BOBCLAW_TEAM", "local-first")
    resp = await client.get("/api/routing-view")
    view = await resp.json()
    assert view["active_team"] == "local-first"
    rows = _by_id(view)
    # local-first worker = local; apex = minimax
    assert rows["worker-deepseek"]["resolved_backend"] == "local"
    assert rows["planner-claude"]["resolved_backend"] == "minimax"


async def test_routing_view_unknown_team_400(client):
    resp = await client.get("/api/routing-view", params={"team": "nope"})
    assert resp.status == 400
    body = await resp.json()
    assert body.get("code") == "unknown_team"


async def test_routing_view_text_format(client):
    resp = await client.get("/api/routing-view", params={"format": "text", "team": "cloud-heavy"})
    assert resp.status == 200
    assert resp.content_type == "text/plain"
    text = await resp.text()
    assert "active_team: cloud-heavy" in text
    assert "FACE" in text and "RESOLVED" in text
    assert "worker-deepseek" in text and "glm_5_2" in text


async def test_routing_view_advertises_live_probe_false(client):
    """JOAT v0: the health-walk probe is a no-op, so resolved_backend is the
    DECLARED mapping. The view advertises live_probe=False so a reader does not
    trust it as health-checked during an outage."""
    resp = await client.get("/api/routing-view")
    view = await resp.json()
    assert view["live_probe"] is False


async def test_routing_view_text_shows_live_probe(client):
    resp = await client.get("/api/routing-view", params={"format": "text"})
    text = await resp.text()
    assert "live_probe:" in text
    assert "not health-checked" in text


# ─── /api/teams — team store CRUD ─────────────────────────────────────────────

async def test_list_teams_endpoint_returns_builtins(client):
    resp = await client.get("/api/teams")
    assert resp.status == 200
    by_name = {t["name"]: t for t in (await resp.json())["items"]}
    assert {"cloud-heavy", "demo-fleet", "local-first"} <= set(by_name)
    assert by_name["demo-fleet"]["builtin"] is True
    assert by_name["demo-fleet"]["roles"]["worker"][0]["backend"] == "deepseek_v4_flash"


async def test_create_team_endpoint_persists_and_routes(client):
    resp = await client.post("/api/teams", json={
        "name": "api-fleet",
        "roles": {"worker": {"backend": "glm_5_2", "escalation_chain": ["kimi_code"]}},
    })
    assert resp.status == 201
    created = await resp.json()
    assert created["name"] == "api-fleet" and created["builtin"] is False

    listing = await (await client.get("/api/teams")).json()
    assert "api-fleet" in {t["name"] for t in listing["items"]}
    # The routing-view can now select it, and it measurably remaps the worker face
    # (worker-deepseek defaults to deepseek_v4_flash → glm_5_2 under api-fleet).
    rv = await (await client.get("/api/routing-view", params={"team": "api-fleet"})).json()
    rows = {f["id"]: f for f in rv["faces"]}
    assert rows["worker-deepseek"]["resolved_backend"] == "glm_5_2"


async def test_create_team_endpoint_validation_400(client):
    resp = await client.post("/api/teams", json={
        "name": "bad", "roles": {"apex": {"backend": "no-such-backend"}},
    })
    assert resp.status == 400
    assert (await resp.json())["code"] == "invalid_team"


async def test_create_team_endpoint_builtin_collision_400(client):
    resp = await client.post("/api/teams", json={
        "name": "demo-fleet", "roles": {"worker": {"backend": "local"}},
    })
    assert resp.status == 400


async def test_delete_team_endpoint(client):
    await client.post("/api/teams", json={
        "name": "temp-fleet", "roles": {"worker": {"backend": "local"}},
    })
    resp = await client.delete("/api/teams/temp-fleet")
    assert resp.status == 200
    names = {t["name"] for t in (await (await client.get("/api/teams")).json())["items"]}
    assert "temp-fleet" not in names


async def test_delete_missing_team_404(client):
    resp = await client.delete("/api/teams/does-not-exist")
    assert resp.status == 404


async def test_backends_endpoint_lists_palette(client):
    resp = await client.get("/api/backends")
    assert resp.status == 200
    data = await resp.json()
    backends = {b["backend"] for b in data["items"]}
    assert {"deepseek_v4_flash", "local", "claude_api"} <= backends
    assert data["roles"] == ["apex", "worker", "critic"]
    one = next(b for b in data["items"] if b["backend"] == "deepseek_v4_flash")
    assert "max_usd_per_worker" in one and "max_fanout_width" in one


async def test_propose_endpoint_returns_proposal(client, monkeypatch):
    async def fake_propose(goal, **kw):
        return {
            "goal": goal, "name": "auto-fleet",
            "roles": {"worker": {"backend": "local", "escalation_chain": []}},
            "raw": "{...}",
        }

    monkeypatch.setattr("core.team_proposer.propose_team", fake_propose)
    resp = await client.post("/api/teams/propose", json={"goal": "cheap local"})
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "auto-fleet"
    assert data["roles"]["worker"]["backend"] == "local"


async def test_refine_endpoint_returns_draft(client, monkeypatch):
    async def fake_refine(message, **kw):
        return {
            "reply": "ok",
            "draft": {"name": "d", "roles": {
                "worker": [{"name": "", "backend": "local", "escalation_chain": []}]}},
            "raw": "{}",
        }

    monkeypatch.setattr("core.team_proposer.refine_team", fake_refine)
    resp = await client.post("/api/teams/refine", json={"message": "cheap", "history": [], "draft": None})
    assert resp.status == 200
    data = await resp.json()
    assert data["reply"] == "ok"
    assert data["draft"]["roles"]["worker"][0]["backend"] == "local"


# ─── /api/profiles — full profile CRUD ────────────────────────────────────────

async def test_profiles_endpoints_crud(client):
    resp = await client.post("/api/profiles", json={
        "name": "council-fast",
        "roles": {"worker": {"backend": "deepseek_v4_flash", "role_prompt": "do the work"}},
        "shape": "fusion",
        "protocol_bounds": {"max_usd": 1.0, "grounding": "off"},
    })
    assert resp.status == 201
    created = await resp.json()
    assert created["shape"] == "fusion"
    assert created["roles"]["worker"][0]["role_prompt"] == "do the work"

    listing = await (await client.get("/api/profiles")).json()
    assert "council-fast" in {p["name"] for p in listing["items"]}

    one = await (await client.get("/api/profiles/council-fast")).json()
    assert one["protocol_bounds"]["grounding"] == "off"

    assert (await client.delete("/api/profiles/council-fast")).status == 200
    assert (await client.get("/api/profiles/council-fast")).status == 404


async def test_create_profile_endpoint_validation_400(client):
    resp = await client.post("/api/profiles", json={
        "name": "bad", "roles": {"worker": {"backend": "local"}}, "shape": "spiral",
    })
    assert resp.status == 400
    assert (await resp.json())["code"] == "invalid_profile"
