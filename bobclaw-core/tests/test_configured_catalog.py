"""Configured lanes: real readers/registries/handlers, fake discovery and transports."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from core import lane_config as lc


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.delenv("BOB_ROSTER_FILE", raising=False)
    monkeypatch.setenv("BOB_LANE_ROOT", str(tmp_path))
    monkeypatch.setattr(lc.shutil, "which", lambda name: "/fake/bin/" + name)
    for key, _ in lc.BACKEND_FIELDS.values():
        monkeypatch.delenv(key, raising=False)
    lc._cached_lane.cache_clear()
    from core import teams
    monkeypatch.setattr(teams, "_custom_teams_dir", None)
    monkeypatch.setattr(teams, "_active_team", None)
    yield tmp_path
    lc._cached_lane.cache_clear()


def env_file(path, values):
    # Same quoted subset used by systemd and runtime. Never shell-evaluated.
    path.write_text("".join(k + "=" + json.dumps(v) + "\n" for k, v in values.items()), encoding="utf-8")


def configure(root, case="c", **overrides):
    values = {}
    if case == "b":
        home = root / "codex-home"
        home.mkdir(exist_ok=True)
        (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-lane-token"}}))
        values = {"BOB_LANE_DEFAULT_BACKEND": "openai_codex", "CODEX_HOME": str(home)}
    elif case == "c":
        env_file(root / "providers.env", {"ANTHROPIC_API_KEY": "fake-a", "DEEPSEEK_API_KEY": "fake-d"})
        values = {"BOB_LANE_DEFAULT_BACKEND": "claude", "ANTHROPIC_MODEL": "lane-anthropic-model",
                  "DEEPSEEK_MODEL": "lane-deepseek-model"}
    values.update(overrides)
    env_file(root / "lane.env", values)
    lc._cached_lane.cache_clear()
    return lc.get_lane()


def decoded(response):
    assert response.status == 200, response.text
    return json.loads(response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("case,backends,default", [
    ("a", [], None), ("b", ["codex_code", "openai_codex"], "openai_codex"),
    ("c", ["claude_api", "deepseek_v4_flash"], "claude_api"),
])
async def test_surface_membership_a_b_c(lane, monkeypatch, case, backends, default):
    from api import server
    from core import teams, team_proposer
    from core.faces.registry import get_registry
    from core.nodes.route import route_node
    from core.tools.teams_tool import list_backends
    from tools import bob_bridge
    configured = configure(lane, case)
    router = SimpleNamespace(discover=AsyncMock(return_value=[]))
    registry = get_registry()
    request = SimpleNamespace(app={server.FACES_KEY: registry, server.ROUTER_KEY: router}, query={})
    faces = decoded(await server.list_faces(request))
    assert [f["id"] for f in faces] == ([] if case == "a" else ["assistant", "builder", "fixer", "orchestrator", "planner", "researcher", "reviewer"])
    assert {f["preferred_backend"] for f in faces} == ({default} if default else set())
    palette = decoded(await server.list_backends_endpoint(request))
    assert palette["roles"] == ["apex", "worker", "critic"]
    assert [b["backend"] for b in palette["items"]] == backends
    from core.config import MAX_WORKER_USD_BY_BACKEND, MAX_FANOUT_WIDTH_BY_BACKEND
    for row in palette["items"]:
        assert row["max_usd_per_worker"] == MAX_WORKER_USD_BY_BACKEND[row["backend"]]
        assert row["max_fanout_width"] == MAX_FANOUT_WIDTH_BY_BACKEND[row["backend"]]
    for endpoint in (server.backends_catalog, server.list_available_models):
        rows = decoded(await endpoint(request))
        assert [r["backend"] for r in rows] == backends
        assert all(r["available"] for r in rows)
        assert [r["model"] for r in rows] == ([] if case == "a" else [None, None] if case == "b" else
                                               ["lane-anthropic-model", "lane-deepseek-model"])
        assert [r["models"] for r in rows] == ([] if case == "a" else [[], []] if case == "b" else
                                                [["lane-anthropic-model"], ["lane-deepseek-model"]])
    view = decoded(await server.routing_view(request))
    assert view["teams"] == []
    assert {f["resolved_backend"] for f in view["faces"]} == ({default} if default else set())
    assert all(f["escalation_chain"] == [] for f in view["faces"])
    assert teams.list_teams() == teams.list_profiles() == []
    assert teams.load_profile("cloud-heavy") is None
    tool_palette = json.loads(list_backends.invoke({}))
    assert [r["backend"] for r in tool_palette["backends"]] == backends
    for b in backends:
        assert b in team_proposer._palette_line()
    assert "minimax" not in team_proposer._palette_line()
    bridge_request = SimpleNamespace(app={bob_bridge.FACES_KEY: registry})
    bridge = decoded(await bob_bridge.models(bridge_request))
    assert [m["id"] for m in bridge["data"]] == ([] if case == "a" else
        ["bob", "bob-assistant", "bob-builder", "bob-fixer", "bob-orchestrator", "bob-planner", "bob-researcher", "bob-reviewer"])
    out = await route_node({"task": "hello", "face_id": "assistant"})
    assert out.get("backend") == default
    assert ("Setup required" in out["error"]) if case == "a" else out.get("error") is None
    assert configured.default_backend == default
    router.discover.assert_not_awaited()
    serialized = json.dumps([faces, palette, view, bridge])
    assert "fake-a" not in serialized and "fake-d" not in serialized and "fake-lane-token" not in serialized


def test_codex_binary_without_lane_auth_is_not_configured(lane, monkeypatch, tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "auth.json").write_text('{"OPENAI_API_KEY":"host-key"}')
    monkeypatch.setenv("CODEX_HOME", str(host))
    assert configure(lane, "a").backends == {}
    env_file(lane / "lane.env", {"CODEX_HOME": str(host)})
    # This is a sibling of the selected lane for the actual isolation check.
    isolated = lane / "isolated"
    isolated.mkdir()
    env_file(isolated / "lane.env", {"CODEX_HOME": str(host)})
    assert lc.read_lane(isolated).backends == {}
    for auth in ({"tokens": {"access_token": "lane-token"}}, {"OPENAI_API_KEY": "lane-key"}):
        home = isolated / "codex-home"
        home.mkdir(exist_ok=True)
        (home / "auth.json").write_text(json.dumps(auth))
        env_file(isolated / "lane.env", {"CODEX_HOME": str(home), "BOB_LANE_DEFAULT_BACKEND": "codex_code"})
        assert set(lc.read_lane(isolated).backends) == ({"codex_code", "openai_codex"} if "tokens" in auth else {"codex_code"})


def test_lane_sources_are_isolated(lane, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "host-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://host-must-not-receive-lane-keys.invalid")
    monkeypatch.setenv("BOB_LANE_DEFAULT_BACKEND", "claude")
    other = lane / "other"
    other.mkdir()
    env_file(other / "providers.env", {"ANTHROPIC_API_KEY": "other-secret"})
    assert configure(lane, "a").backends == {}
    assert lc.get_lane().default_backend is None
    env_file(lane / "providers.env", {"ANTHROPIC_API_KEY": "lane-key", "ANTHROPIC_MODEL": "provider-model"})
    env_file(lane / "lane.env", {"ANTHROPIC_MODEL": "lane-model", "BOB_LANE_DEFAULT_BACKEND": "claude"})
    selected = lc.read_lane(lane)
    assert selected.backends["claude_api"].model == "provider-model"
    # Fresh interpreter also proves core.config never imports repo dotenv or
    # inherited credentials. Neither file is read or changed by this test.
    env = dict(os.environ, BOB_LANE_ROOT=str(other / "empty"), PYTHONPATH=str(Path(__file__).parents[1]))
    result = subprocess.run([sys.executable, "-X", "utf8", "-c",
        'from core.config import config; from core.lane_config import get_lane; '
        'from core.lane_config import transport_defaults; '
        'assert not config.ANTHROPIC_API_KEY; assert not config.CODEX_HOME.endswith(".codex"); '
        'assert config.ANTHROPIC_BASE_URL == transport_defaults()["ANTHROPIC_BASE_URL"]; '
        'assert not get_lane().backends'], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_default_alias_and_second_provider(lane):
    cfg = configure(lane, "c")
    assert cfg.default_backend == "claude_api" and cfg.default_error() is None
    values = lc.read_env(lane / "lane.env")
    values.pop("BOB_LANE_DEFAULT_BACKEND")
    env_file(lane / "lane.env", values)
    assert "select" in lc.read_lane(lane).default_error()
    values["BOB_LANE_DEFAULT_BACKEND"] = "minimax"
    env_file(lane / "lane.env", values)
    assert "needs provider minimax" in lc.read_lane(lane).default_error()


@pytest.mark.asyncio
async def test_local_catalog_requires_chat_models(lane):
    from core.backends.local_router import LocalBackendInfo
    from core.faces.registry import get_registry
    cfg = configure(lane, "a", OLLAMA_URL="http://127.0.0.1:12001", BOB_LANE_DEFAULT_BACKEND="local")
    registry = get_registry()
    assert registry.list_faces() == []
    discover = AsyncMock(return_value=[])
    router = SimpleNamespace(discover=discover)
    assert await lc.catalog(router) == []
    discover.return_value = [LocalBackendInfo("ollama", "http://127.0.0.1:12001", ["text-embedding-only"])]
    assert await lc.catalog(router) == []
    discover.side_effect = OSError("offline")
    assert await lc.catalog(router) == []
    discover.side_effect = None
    discover.return_value = [LocalBackendInfo("ollama", "http://127.0.0.1:12001", ["chat-model", "reranker"]),
                             LocalBackendInfo("household", "http://127.0.0.1:9999", ["unpermitted-model"])]
    rows = await lc.catalog(router)
    assert len(rows) == 1 and rows[0]["models"] == ["chat-model"]
    assert len(registry.list_faces()) == 3
    assert cfg.default_error() is None


def test_registry_consistency(lane):
    from api import server
    from core.faces.registry import get_registry, get_default_registry, FaceRegistry
    from tools.bob_bridge import build_app, FACES_KEY
    configure(lane, "b")
    # The bridge's factory is also the HTTP/routing factory.
    assert get_registry() is get_default_registry()
    assert [f.model_dump() for f in FaceRegistry().all_faces()] == [f.model_dump() for f in get_registry().all_faces()]
    assert build_app()[FACES_KEY] is get_registry()
    assert server.build_app()[server.FACES_KEY] is get_registry()


@pytest.mark.asyncio
async def test_configured_routing_has_no_implicit_fallback(lane, monkeypatch):
    from core.nodes import route, execute, critic, panel, council, synthesize
    from core.context.compactor import default_llm
    from core.verify.postcondition import decorrelated_critic_backend, PostConditionError
    from core import team_proposer
    configure(lane, "c")
    from core.mcp_tools.profile_routing import class_to_face
    assert {class_to_face(c) for c in ("worker", "route_cheap", "plan_code", "synth_mid", "plan_concept", "synth_deep")} == {
        "assistant", "researcher", "reviewer"}
    calls = []
    async def failing(messages, backend, *args):
        calls.append(backend)
        raise RuntimeError("original-provider-failure")
    monkeypatch.setattr(execute, "_stream_and_collect", failing)
    monkeypatch.setattr(execute, "_append_agent_turn_event", AsyncMock())
    monkeypatch.setattr(execute, "_is_tool_enabled", lambda *args: False)
    for task in ("please plan a research project", "please plan implementation code", "hello"):
        state = await route.route_node({"task": task})
        assert state["backend"] == "claude_api" and state["escalation_backend"] is None
    for backend in ("minimax", "deepseek_v4", "kimi_platform", "claude_code"):
        out = await route.route_node({"task": "hello", "backend_override": backend})
        assert "needs provider " + backend in out["error"]
        out = await execute.execute_node({"backend": backend})
        assert "needs provider " + backend in out["error"]
    assert calls == []
    from core.nodes.decompose import decompose_node
    planner = AsyncMock()
    monkeypatch.setattr("core.nodes.decompose._call_llm", planner)
    assert await decompose_node({"task": "plan a complex implementation", "error": "Setup required"}) == {}
    planner.assert_not_awaited()
    out = await execute.execute_node({"backend": "claude_api", "messages": []})
    assert out["error"] == "original-provider-failure" and calls == ["claude_api"]
    calls.clear()
    monkeypatch.setattr(critic, "_send_to_backend", failing)
    verdict, reasons = await critic.run_critic("task", "result", "minimax", fallback_chain=[])
    assert verdict == "none" and "needs provider minimax" in reasons[0] and calls == []
    with pytest.raises(PostConditionError):
        decorrelated_critic_backend("claude_api")
    with pytest.raises(lc.LaneConfigurationError, match="needs provider minimax"):
        await default_llm("minimax")([])
    with pytest.raises(RuntimeError, match="original-provider-failure"):
        await default_llm()([])
    assert calls == ["claude_api"]
    calls.clear()
    monkeypatch.setattr(panel, "_send_to_backend", failing)
    monkeypatch.setattr(synthesize, "_send_to_backend", failing)
    for fn in (panel.panel_dispatch_node, council.council_node, synthesize.synthesize_node):
        with pytest.raises(lc.LaneConfigurationError, match="council seats"):
            result = fn({"task": "topic"})
            if hasattr(result, "__await__"):
                await result
    assert calls == []
    send = AsyncMock(return_value='{"roles": {"worker": {"backend":"minimax"}}}')
    proposal = await team_proposer.propose_team("goal", send=send)
    assert proposal["roles"] == {}
    assert send.await_args.args[1] == "claude_api"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,kind", [
    ("opencode_serve", "unavailable"), ("claude_code", "throttled"), ("agy_code", "throttled"),
    ("codex_code", "throttled"), ("kimi_code", "429"), ("pi_code", "429"), ("pi_code", "error"),
])
async def test_each_execute_failure_preserves_original(lane, monkeypatch, backend, kind):
    from core.nodes import execute as ex
    from aiohttp import ClientResponseError
    from core.backends.opencode_pool import NoOpenCodeAvailable, _pool
    from core.backends.claude_code import ClaudeCodeThrottled
    from core.backends.agy_code import AgyThrottled
    from core.backends.codex_code import CodexThrottled
    from core.backends.pi_code import PiError
    cfg = configure(lane, "c")
    # Explicit enrolled-seat fixture, independent of adapter auth implementation.
    cfg.backends[backend] = lc.LaneBackend(backend, "cli")
    monkeypatch.setattr(ex, "_is_tool_enabled", lambda *args: False)
    monkeypatch.setattr(ex, "_face_spawn_posture", lambda p, s: p)
    monkeypatch.setattr(ex, "_append_agent_turn_event", AsyncMock())
    failures = {"opencode_serve": NoOpenCodeAvailable, "claude_code": ClaudeCodeThrottled,
                "agy_code": AgyThrottled, "codex_code": CodexThrottled}
    error = (failures[backend]("original-provider-failure") if backend in failures else
             ClientResponseError(MagicMock(real_url="test"), (), status=429, message="original-provider-failure")
             if kind == "429" else PiError("original-provider-failure"))
    stream = AsyncMock(side_effect=error if backend in {"kimi_code", "pi_code"} else None)
    monkeypatch.setattr(ex, "_stream_and_collect", stream)
    if backend == "opencode_serve":
        monkeypatch.setattr(_pool, "dispatch", AsyncMock(side_effect=error))
    elif backend in failures:
        target = {"claude_code": "core.backends.claude_code.ClaudeCodeClient",
                  "agy_code": "core.backends.agy_code.AntigravityClient",
                  "codex_code": "core.backends.codex_code.CodexCodeClient"}[backend]
        from unittest.mock import patch
        with patch(target) as client:
            client.return_value.chat = AsyncMock(side_effect=error)
            out = await ex.execute_node({"backend": backend, "messages": []})
    if backend not in {"claude_code", "agy_code", "codex_code"}:
        out = await ex.execute_node({"backend": backend, "messages": []})
    assert "original-provider-failure" in out["error"]
    assert [c.args[1] for c in stream.await_args_list] == ([backend] if backend in {"kimi_code", "pi_code"} else [])


@pytest.mark.asyncio
async def test_team_requirements_before_spend(lane, monkeypatch):
    from core import teams
    from core.nodes import route, execute
    configure(lane, "c")
    directory = lane / "teams"
    directory.mkdir()
    (directory / "saved.yaml").write_text(yaml.safe_dump({"name": "saved", "roles": {
        "worker": {"backend": "claude_api", "escalation_chain": []},
        "critic": {"backend": "minimax", "escalation_chain": []}}}))
    send = AsyncMock()
    monkeypatch.setattr(execute, "_stream_and_collect", send)
    monkeypatch.setattr(execute, "_append_agent_turn_event", AsyncMock())
    assert teams.list_teams()[0]["roles"]["critic"][0]["backend"] == "minimax"
    for state in ({"team": "saved"}, {"profile_name": "saved"}, {"team": "cloud-heavy"}):
        out = await route.route_node({"task": "hello", **state})
        assert "needs provider" in out["error"]
    out = await execute.execute_node({"backend": "claude_api", "team": "saved"})
    assert "needs provider minimax" in out["error"]
    send.assert_not_awaited()


def test_lapsed_and_missing_models_stay_visible(lane):
    cfg = configure(lane, "c", BOB_UNAVAILABLE_BACKENDS="claude_api", DEEPSEEK_MODEL="")
    assert set(cfg.backends) == {"claude_api", "deepseek_v4_flash"}
    assert cfg.backends["claude_api"].reason == "entitlement_lapsed"
    assert cfg.backends["deepseek_v4_flash"].reason == "model_configuration_required"
    assert cfg.default_error()
