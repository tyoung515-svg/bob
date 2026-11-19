"""Configured lanes through the real gateway; core readers with fake transports.

Only loopback test servers are used. No inference, discovery, login, or live DB.
"""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import create_access_token, create_agent_token
from config import config
from core import lane_config as lc
from core.permissions import verify_scope
from routers import agent_services as svc
from tests.fake_pool import InMemoryPostgresPool


def write_env(path, values):
    path.write_text("".join(f"{k}={json.dumps(v)}\n" for k, v in values.items()),
                    encoding="utf-8", newline="\n")


@pytest.fixture
async def lane_gateway(tmp_path, monkeypatch):
    monkeypatch.delenv("BOB_ROSTER_FILE", raising=False)
    lane = tmp_path / "lane"
    lane.mkdir()
    monkeypatch.setenv("BOB_LANE_ROOT", str(lane))
    monkeypatch.setenv("OPENROUTER_API_KEY", "host-only-openrouter-key")
    monkeypatch.setattr(lc.shutil, "which", lambda name: "/fake/bin/" + name)
    lc._cached_lane.cache_clear()
    monkeypatch.setattr(config, "JWT_SECRET", "configured-gateway-test-secret-long-enough")
    monkeypatch.setattr(config, "BOBCLAW_SECRET", "configured-scope-test-secret")
    monkeypatch.setattr(config, "AGENT_INNER_GATES", "strict")
    monkeypatch.setattr(config, "AUDIT_LOG_ENABLED", False)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 10000)
    monkeypatch.setattr(config, "RATE_LIMIT_BURST", 1000)
    probe = AsyncMock(side_effect=AssertionError("household probe must not run"))
    monkeypatch.setattr(svc, "local_status", probe)

    # The unrelated work-memory router imports Unix flock at module load. Its
    # operations are outside these tests and must fail if accidentally reached.
    if sys.platform == "win32" and "fcntl" not in sys.modules:
        flock = ModuleType("fcntl")
        def forbidden_flock(*args):
            raise AssertionError("work-memory flock is outside this test")
        flock.flock = forbidden_flock
        monkeypatch.setitem(sys.modules, "fcntl", flock)

    from api import server
    import gateway
    from app_state import POSTGRES_POOL_KEY
    from core.faces.registry import get_registry
    monkeypatch.setattr(gateway, "init_db", AsyncMock())
    monkeypatch.setattr(gateway, "close_postgres_pool", AsyncMock())
    monkeypatch.setattr(gateway, "close_redis", AsyncMock())
    original_setup = svc.setup
    monkeypatch.setattr(svc, "setup", lambda app: original_setup(app, root=tmp_path / "services"))
    discovery = AsyncMock(side_effect=AssertionError("no local endpoint enrolled"))
    core_app = web.Application()
    core_app[server.ROUTER_KEY] = SimpleNamespace(discover=discovery)
    core_app[server.FACES_KEY] = get_registry()
    faults, reads, turns = {}, [], []
    handlers = {
        "/api/faces": server.list_faces,
        "/api/backends": server.list_backends_endpoint,
        "/api/backends/catalog": server.backends_catalog,
        "/api/models/available": server.list_available_models,
        "/api/agent-services/definitions": server.agent_services_definitions,
    }
    async def core_read(request):
        reads.append(request.path)
        fault = faults.get(request.path)
        if isinstance(fault, int):
            return web.json_response({"error": "fixture unavailable"}, status=fault)
        if fault is not None:
            return web.json_response(fault)
        return await handlers[request.path](request)
    for path in handlers:
        core_app.router.add_get(path, core_read)

    async def core_chat(request):
        turns.append(await request.json())
        events = [{"type": "chunk", "content": "fixture answer"},
                  {"type": "message_complete", "tokens_in": 3, "tokens_out": 2}]
        return web.Response(text="".join(json.dumps(e) + "\n" for e in events))
    core_app.router.add_post("/api/chat", core_chat)
    core_client = TestClient(TestServer(core_app))
    await core_client.start_server()
    monkeypatch.setattr(config, "CORE_URL", str(core_client.make_url("/")).rstrip("/"))
    pool = InMemoryPostgresPool()
    client = TestClient(TestServer(gateway.build_app({POSTGRES_POOL_KEY: pool})))
    await client.start_server()

    def configure(case, **overrides):
        values = {}
        if case == "b":
            home = lane / "codex-home"
            home.mkdir()
            (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-lane-token"}}),
                                            encoding="utf-8", newline="\n")
            values = {"BOB_LANE_DEFAULT_BACKEND": "openai_codex", "CODEX_HOME": str(home)}
        elif case == "c":
            write_env(lane / "providers.env", {"ANTHROPIC_API_KEY": "fake-a", "DEEPSEEK_API_KEY": "fake-d"})
            values = {"BOB_LANE_DEFAULT_BACKEND": "claude", "ANTHROPIC_MODEL": "lane-anthropic-model",
                      "DEEPSEEK_MODEL": "lane-deepseek-model"}
        write_env(lane / "lane.env", {**values, **overrides})
        lc._cached_lane.cache_clear()

    yield SimpleNamespace(client=client, core=core_client, configure=configure, probe=probe,
                          discovery=discovery, faults=faults, reads=reads, turns=turns, pool=pool)
    await client.close()
    await core_client.close()
    lc._cached_lane.cache_clear()
    probe.assert_not_awaited()
    discovery.assert_not_awaited()


def headers(*, faces=None, sub="admin", scope=None):
    token = (create_access_token(sub) if faces is None else
             create_agent_token(sub, faces=faces, scope=scope))
    return {"Authorization": "Bearer " + token}


async def get_json(client, path, **kwargs):
    response = await client.get(path, **kwargs)
    assert response.status == 200, await response.text()
    return await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,backends,default", [
    ("a", [], None), ("b", ["codex_code", "openai_codex"], "openai_codex"),
    ("c", ["claude_api", "deepseek_v4_flash"], "claude_api"),
])
async def test_proxy_and_capabilities_a_b_c(lane_gateway, case, backends, default):
    g = lane_gateway
    g.configure(case)
    expected_faces = [] if case == "a" else [
        "assistant", "builder", "fixer", "orchestrator", "planner", "researcher", "reviewer"]
    for path, core_path in [("/faces", "/api/faces"), ("/backends", "/api/backends"),
                            ("/models/available", "/api/models/available"),
                            ("/backends/catalog", "/api/backends/catalog")]:
        assert (await g.client.get(path)).status == 401
        assert await get_json(g.client, path, headers=headers()) == await get_json(g.core, core_path)
    faces = await get_json(g.client, "/faces", headers=headers())
    assert [f["id"] for f in faces] == expected_faces
    assert {f["preferred_backend"] for f in faces} == ({default} if default else set())
    palette = await get_json(g.client, "/backends", headers=headers())
    assert [b["backend"] for b in palette["items"]] == backends
    assert palette["roles"] == ["apex", "worker", "critic"]
    models = await get_json(g.client, "/models/available", headers=headers())
    assert [m["backend"] for m in models] == backends
    assert [m["model"] for m in models] == ([] if case == "a" else [None, None] if case == "b" else
                                            ["lane-anthropic-model", "lane-deepseek-model"])
    assert (await g.client.get("/capabilities")).status == 401
    caps = await get_json(g.client, "/capabilities", headers=headers())
    assert [f["id"] for f in caps["faces"]] == expected_faces
    assert [b["backend"] for b in caps["backends"]] == backends
    assert caps["capabilities"]["available_backends"] == backends
    assert caps["capabilities"]["face_count"] == len(expected_faces)
    assert caps["capabilities"]["backend_count"] == len(backends)
    for row, model, cost in zip(caps["backends"], models, palette["items"]):
        assert row == {**cost, "model": model["model"], "available": model["available"],
                       **{key: model[key] for key in ('provider_label', 'choices', 'tool_capable', 'billing')}}
    # A stale broad cost palette must not re-enroll an adapter.
    g.faults["/api/backends"] = {**palette, "items": [*palette["items"], {"backend": "minimax"}]}
    caps = await get_json(g.client, "/capabilities", headers=headers())
    assert [b["backend"] for b in caps["backends"]] == backends
    g.faults["/api/models/available"] = 503
    caps = await get_json(g.client, "/capabilities", headers=headers())
    assert caps["backends"] == [] and "models: HTTP 503" in caps["warnings"]
    g.faults.clear()
    # Preserve proxy error handling and the capability component degradation.
    g.faults["/api/faces"] = 503
    assert (await g.client.get("/faces", headers=headers())).status == 502
    caps = await get_json(g.client, "/capabilities", headers=headers())
    assert caps["faces"] == [] and "faces: HTTP 503" in caps["warnings"]
    assert [b["backend"] for b in caps["backends"]] == backends
    g.faults.update({"/api/backends": 503, "/api/models/available": 503})
    assert (await g.client.get("/capabilities", headers=headers())).status == 502
    assert g.turns == []
    serialized = json.dumps([faces, palette, models, caps])
    assert all(secret not in serialized for secret in ("fake-a", "fake-d", "fake-lane-token", "host-only-openrouter-key"))


@pytest.mark.asyncio
@pytest.mark.parametrize("case,overrides,bindings", [
    ("a", {}, []), ("b", {}, []),
    ("b", {"CODEX_MODEL": "lane-codex-model"}, [("codex_code", "lane-codex-model"), ("openai_codex", "lane-codex-model")]),
    ("c", {}, [("claude_api", "lane-anthropic-model"), ("deepseek_v4_flash", "lane-deepseek-model")]),
    ("c", {"DEEPSEEK_MODEL": "deepseek-v4-flash"},
     [("claude_api", "lane-anthropic-model"), ("deepseek_v4_flash", "deepseek-v4-flash")]),
    ("c", {"BOB_UNAVAILABLE_BACKENDS": "claude_api"},
     [("claude_api", "lane-anthropic-model"), ("deepseek_v4_flash", "lane-deepseek-model")]),
])
async def test_agent_services_from_effective_roster(lane_gateway, case, overrides, bindings):
    g = lane_gateway
    g.configure(case, **overrides)
    assert (await g.client.get("/agent-services/catalog")).status == 401
    assert (await g.client.get("/v1/models")).status == 401
    catalog = await get_json(g.client, "/agent-services/catalog", headers=headers(faces=["researcher"]))
    models = await get_json(g.client, "/v1/models", headers=headers(faces=["researcher"]))
    definitions = await get_json(g.core, "/api/agent-services/definitions")
    specs = {r["id"]: {k: v for k, v in r.items() if k != "id"} for r in catalog["model_faces"]}
    assert specs == definitions["models"] == {r["id"]: r["bob"] for r in models["data"]}
    assert [(r["backend"], r["model"]) for r in specs.values()] == bindings
    for spec in specs.values():
        if spec['backend']=='deepseek_v4_flash' and spec['model']=='deepseek-v4-flash':
            assert spec['reported_model_names']==['deepseek-flash']
        else:
            assert 'reported_model_names' not in spec
    assert catalog["teams"] == [] and catalog["worker_choices"] == {}
    assert "local_backend" not in catalog and "openrouter" not in catalog
    assert all(pin not in specs for pin in ("bob-gpt-sol-61", "bob-gpt-luna", "bob-ds-flash", "bob-qwen-strata"))
    assert (await g.client.post("/agent-services/runs", headers=headers(faces=["researcher"]),
                               json={"team": "balance-budget", "task": "fixture"})).status == 404
    request_body = {"model": "bob-ds-flash", "messages": [{"role": "user", "content": "hi"}]}
    assert (await g.client.post("/v1/chat/completions", headers=headers(faces=["researcher"]),
                               json=request_body)).status == 404
    for alias, spec in specs.items():
        request_body["model"] = alias
        assert (await g.client.post("/v1/chat/completions", headers=headers(faces=["assistant"]),
                                   json=request_body)).status == 403
        before = len(g.turns)
        response = await g.client.post("/v1/chat/completions", headers=headers(faces=["researcher"]),
                                       json={**request_body, "tools": []})
        assert response.status == 422 and len(g.turns) == before
        if not spec["enabled"]:
            assert spec["disabled_reason"] == "entitlement_lapsed"
            response = await g.client.post("/v1/chat/completions", headers=headers(faces=["researcher"]),
                                           json=request_body)
            assert response.status == 409 and len(g.turns) == before
            assert (await response.json())["error"]["code"] == "model_face_unavailable"
            continue
        scope = {"may_touch": ["lane-evidence"]}
        snapshot_reads = g.reads.count("/api/agent-services/definitions")
        response = await g.client.post("/v1/chat/completions", headers=headers(faces=["researcher"], scope=scope),
                                       json=request_body)
        assert response.status == 200, await response.text()
        result = await response.json()
        assert result["bob"]["model_pin"] == spec["model"]
        assert result["choices"][0]["message"]["content"] == "fixture answer"
        assert g.reads.count("/api/agent-services/definitions") == snapshot_reads + 1
        assert len(g.turns) == before + 1
        turn = g.turns[-1]
        assert (turn["backend"], turn["model"]) == (spec["backend"], spec["model"])
        assert turn["face_id"] == "researcher" and turn["agent_service_single_turn"] is True
        assert turn["scope"] == scope and verify_scope(scope, turn["scope_vouch"], config.BOBCLAW_SECRET)
        # Existing conversation ownership remains authoritative.
        response = await g.client.post("/v1/chat/completions", headers=headers(faces=["researcher"], sub="other"),
                                       json={**request_body, "conversation_id": result["bob"]["conversation_id"]})
        assert response.status == 404 and len(g.turns) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [503, [], {"mode": "deployment"},
                                    {"mode": "configured", "models": {}, "teams": {}}])
async def test_snapshot_failure_never_falls_back(lane_gateway, fault):
    g = lane_gateway
    g.configure("c")
    g.faults["/api/agent-services/definitions"] = fault
    for path in ("/agent-services/catalog", "/v1/models"):
        response = await g.client.get(path, headers=headers())
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "agent_services_catalog_unavailable"
    for path, body in [("/v1/chat/completions", {"model": "bob-ds-flash", "messages": []}),
                       ("/agent-services/runs", {"team": "balance-budget", "task": "fixture"})]:
        assert (await g.client.post(path, headers=headers(faces=["researcher"]), json=body)).status == 503
    assert g.turns == [] and g.pool.messages == []


@pytest.mark.asyncio
async def test_cli_uses_server_default_without_a_pin(monkeypatch):
    """Exercise the CLI framing without secrets, a login, or a socket."""
    path = Path(__file__).resolve().parents[2] / "scripts/bobclaw_cli.py"
    spec = importlib.util.spec_from_file_location("configured_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    frames = []
    class Context:
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self.value
        async def __aexit__(self, *args): pass
    class Socket:
        async def send_json(self, frame): frames.append(frame)
    class Session:
        def get(self, *args, **kwargs): return Context(SimpleNamespace(status=200))
        def ws_connect(self, *args, **kwargs): return Context(Socket())
    monkeypatch.setattr(cli, "dotenv_values", lambda path: {"BOBCLAW_PASSWORD": "fake", "TOTP_SECRET": "fake"})
    monkeypatch.setattr(cli, "_get_token", AsyncMock(return_value="fake-token"))
    monkeypatch.setattr(cli.aiohttp, "ClientSession", lambda **kwargs: Context(Session()))
    stream = AsyncMock()
    monkeypatch.setattr(cli, "_stream", stream)
    monkeypatch.setattr(sys, "argv", [str(path), "--conversation", "fixture", "-m", "hello"])
    await cli.main()
    assert frames == []
    stream.assert_awaited_once()
    # A backend-only override must not inherit a model pin from another provider.
    monkeypatch.setattr(sys, "argv", [str(path), "--conversation", "fixture", "-m", "hello", "--backend", "claude_api"])
    await cli.main()
    assert frames == [{"type": "switch_model", "model": "", "backend": "claude_api", "conversation_id": "fixture"}]
