"""PA1: runtime prompt captures and configured-lane topology/profile contracts.

All transports and event persistence are fakes. No provider calls or live writes.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from core import lane_config as lc, permissions, teams
from core.faces.registry import FaceRegistry, get_default_registry
from core.nodes import execute, route, decompose, dispatch
from core.tools.registry import NATIVE_TOOLS, PIPE_OPERATIONS, PipeCallArgs, skill_view, get_server_time, _skill_face_id
from test_configured_catalog import lane, configure
from core.tools.pc_tools import PC_TOOL_NAMES

from core.tools.workflows import WORKFLOW_TOOLS
EXPECTED_TOOLS = {t.name for t in WORKFLOW_TOOLS} | set(PC_TOOL_NAMES) | {
    "bob_guide", "memory_save", "memory_recall", "web_search", "web_fetch", "pipe_call", "ask_operator",
    "list_backends", "create_project", "create_team", "skill_list", "skill_view", "get_server_time",
    "conv_search", "conv_expand", "conversations_find", "conversations_read", "memory_retire", "memory_correct",
}


@pytest.fixture
def configured(lane, monkeypatch):
    # Prompt/topology tests do not test tokenization. Keep standalone collection
    # offline too, independent of whether another suite initialized memory first.
    monkeypatch.setattr('tiktoken.get_encoding',lambda *a,**k:SimpleNamespace(
        encode=lambda text,*a,**k:list(text.encode()),decode=lambda tokens:bytes(tokens).decode()))
    cfg = configure(lane, "c", BOB_LANE_DEFAULT_BACKEND="deepseek_v4_flash")
    monkeypatch.delenv("BOB_FACE_PROMPTS", raising=False)
    monkeypatch.setattr(execute, "_append_agent_turn_event", AsyncMock())
    monkeypatch.setattr(execute, "bind_flight_from_state", lambda state: None)
    monkeypatch.setattr(execute, "task_requires_approval", lambda task: False)
    monkeypatch.setattr(execute, "_get_stream_writer", lambda: None)
    monkeypatch.setattr(execute, "_check_escalation_pin", AsyncMock(return_value=None))
    return cfg


def turn(**changes):
    return dict(task="hello", backend="deepseek_v4_flash", face_id="assistant",
                messages=[], **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('face', ['assistant', 'planner'])
async def test_workflow_context_binds_only_verified_invocation_and_resets(configured, monkeypatch, face):
    import time
    from core.tools import workflows, workflow_context
    from core.execution.models import Authority
    from core.config import config
    seen = []
    async def invoke(operation, args):
        seen.append(workflow_context.current.get())
        return {'templates': []}
    monkeypatch.setattr(workflows, 'call', invoke)
    class Model:
        rounds = 0
        def bind_tools(self, tools): return self
        async def ainvoke(self, messages):
            self.rounds += 1
            return (AIMessage(content='', tool_calls=[{'name': 'workflows_catalog', 'args': {}, 'id': 'workflow-call'}])
                if self.rounds == 1 else AIMessage(content='Catalog checked.'))
    model = Model()
    monkeypatch.setattr(execute, '_build_tool_model', lambda *args, **kwargs: model)
    now = int(time.time())
    value = {'authority': Authority('lane', 'owner', 'owner', {}, 'portal').as_dict(),
        'conversation_id': 'conversation', 'face_id': face, 'iat': now, 'exp': now+600}
    monkeypatch.setattr(config, 'BOBCLAW_SECRET', 'workflow-prompt-fixture-secret')
    state = dict(turn(), face_id=face, user_id='owner', conversation_id='conversation', workflow_context=value,
        workflow_context_vouch=workflow_context.signature(value, config.BOBCLAW_SECRET))
    result = await execute.execute_node(state)
    assert result.get('error') is None and seen[0][0] == value
    assert workflow_context.current.get() is None
    model.rounds = 0
    state['workflow_context_vouch'] = '0'*64
    await execute.execute_node(state)
    assert seen[-1] is None and workflow_context.current.get() is None


def card_from(contents):
    return json.loads(next(s.split("\n", 1)[1] for s in contents
                          if s.startswith("Current runtime capability card:\n")))


@pytest.mark.asyncio
@pytest.mark.parametrize('face', ['assistant', 'planner'])
async def test_native_prompt_once_each_call_and_never_persisted(configured, monkeypatch, face):
    captures, bound = [], []
    expected_tools = EXPECTED_TOOLS - ({"memory_retire", "memory_correct"} if face == "planner" else set())

    class Transport:
        def bind_tools(self, tools):
            bound.extend(t.name for t in tools)
            return self

        async def ainvoke(self, messages):
            captures.append([m.content for m in messages])
            if len(captures) == 1:
                return AIMessage(content="", tool_calls=[dict(
                    name="skill_view", id="setup", args={"skill_id": "bob.setup", "version": 1})])
            return AIMessage(content="one answer")

    monkeypatch.setattr(execute, "_build_tool_model", lambda backend: Transport())
    state = dict(turn(), face_id=face)
    state.update(locale="zh-Hans", project_instructions="PROJECT_MARKER",
                 recalled_facts=[SimpleNamespace(body={"text": "RECALL_MARKER"})])
    state["messages"] = [{"role": "system", "content": "Prior conversation:\nHISTORY_MARKER"}]
    original = deepcopy(state)
    result = await execute.execute_node(state)
    prompt = get_default_registry().get_system_prompt(face)
    assert result.get("error") is None
    assert len(captures) == 2 and set(bound) == expected_tools
    for contents in captures:
        assert sum(s.count(prompt) for s in contents) == 1
        assert contents[0] == prompt
        assert contents[2] == execute.LOCALE_DIRECTIVE["zh-Hans"]
        assert "PROJECT_MARKER" in str(contents) and "RECALL_MARKER" in str(contents)
        assert "HISTORY_MARKER" in str(contents)
        card = card_from(contents)
        assert set(card["native_tools"]) == expected_tools
        assert card["chat_pipe_operations"] == sorted(PIPE_OPERATIONS)
        assert card["selected_backend"] == "deepseek_v4_flash"
        assert card["worker_dispatch_exposed"] is False
    assert state == original
    assert prompt not in str(result)
    assert "Current runtime capability card" not in str(execute._append_agent_turn_event.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["deepseek_v4_flash", "local", "openai_codex"])
async def test_plain_stream_prompt_once(configured, monkeypatch, backend):
    # Every configured face now has PC request tools. This test deliberately
    # exercises the plain-stream prompt transport rather than the tool loop.
    monkeypatch.setattr(execute, '_is_tool_enabled', lambda *a: False)
    configured.backends[backend] = lc.LaneBackend(backend, "cloud")
    captured = []
    async def stream(messages, *args):
        captured.extend(m["content"] for m in messages)
        yield "answer"
    monkeypatch.setattr(execute, "_stream_to_backend", stream)
    result = await execute.execute_node(dict(turn(), backend=backend, face_id="reviewer"))
    prompt = get_default_registry().get_system_prompt("reviewer")
    assert result.get("error") is None and captured[0] == prompt
    assert sum(s.count(prompt) for s in captured) == 1
    assert card_from(captured)["native_tools"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_claude_system_join_once(configured, monkeypatch, streaming):
    from core.backends.claude import ClaudeClient
    captured = []
    async def stream(self, messages, **kwargs):
        captured.append((messages, kwargs["system"]))
        yield "answer"
    async def chat(self, messages, **kwargs):
        captured.append((messages, kwargs["system"]))
        return {"content": [{"type": "text", "text": "answer"}]}
    monkeypatch.setattr(ClaudeClient, "stream_chat", stream)
    monkeypatch.setattr(ClaudeClient, "chat", chat)
    monkeypatch.setattr(execute, "_is_tool_enabled", lambda *args: False)
    if not streaming:
        async def collect(messages, backend, model, writer):
            return await execute._default_send_to_backend(messages, backend, model)
        monkeypatch.setattr(execute, "_stream_and_collect", collect)
    result = await execute.execute_node(dict(turn(), backend="claude_api", face_id="reviewer"))
    prompt = get_default_registry().get_system_prompt("reviewer")
    assert result.get("error") is None
    assert len(captured) == 1 and captured[0][1].startswith(prompt)
    assert captured[0][1].count(prompt) == 1
    assert all(m["role"] != "system" for m in captured[0][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,target", [
    ("claude_code", "core.backends.claude_code.ClaudeCodeClient"),
    ("agy_code", "core.backends.agy_code.AntigravityClient"),
    ("codex_code", "core.backends.codex_code.CodexCodeClient"),
    ("kimi_cli", "core.backends.kimi_cli.KimiCliClient"),
    ("pi_code", "core.backends.pi_code.PiCodeClient"),
])
async def test_cli_prompt_once(configured, monkeypatch, backend, target):
    from importlib import import_module
    module, name = target.rsplit(".", 1)
    captured = []
    class Client:
        last_session_id = None
        def __init__(self, *args, **kwargs):
            pass
        async def chat(self, **kwargs):
            captured.append(kwargs["prompt"])
            return {"text": "answer"}
        async def stream_chat(self, **kwargs):
            captured.append(kwargs["prompt"])
            yield "answer"
    configured.backends[backend] = lc.LaneBackend(backend, "cli")
    monkeypatch.setattr(import_module(module), name, Client)
    monkeypatch.setattr(execute, "_maybe_capture_cc_edit", AsyncMock(return_value=None))
    result = await execute.execute_node(dict(turn(), backend=backend, face_id="reviewer"))
    prompt = get_default_registry().get_system_prompt("reviewer")
    assert result.get("error") is None
    assert len(captured) == 1 and captured[0].count(prompt) == 1
    assert captured[0].startswith(prompt)


@pytest.mark.asyncio
async def test_workspace_transport_prompt_once(configured, monkeypatch):
    from core.backends.opencode_pool import _pool
    configured.backends["opencode_serve"] = lc.LaneBackend("opencode_serve", "cli")
    send = AsyncMock(return_value="answer")
    monkeypatch.setattr(_pool, "dispatch", send)
    result = await execute.execute_node(dict(turn(), backend="opencode_serve", face_id="reviewer"))
    prompt = get_default_registry().get_system_prompt("reviewer")
    assert result.get("error") is None
    assert sum(m["content"].count(prompt) for m in send.call_args.args[0]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", [False, True])
async def test_legacy_rollout_gate_and_stateless_exclusion(monkeypatch, opt_in):
    # Every face now has the guide; explicitly exercise plain transport here.
    monkeypatch.setattr(execute, "_is_tool_enabled", lambda *args: False)
    if opt_in:
        monkeypatch.setenv("BOB_FACE_PROMPTS", "1")
    else:
        monkeypatch.delenv("BOB_FACE_PROMPTS", raising=False)
    captured = []
    async def stream(messages, *args):
        captured.append(deepcopy(messages))
        yield "answer"
    monkeypatch.setattr(execute, "_stream_to_backend", stream)
    monkeypatch.setattr(execute, "_append_agent_turn_event", AsyncMock())
    monkeypatch.setattr(execute, "_get_stream_writer", lambda: None)
    prompt = get_default_registry().get_system_prompt("reviewer")
    for service in (False, True):
        result = await execute.execute_node(dict(turn(), face_id="reviewer", agent_service_single_turn=service))
        assert result.get("error") is None
        assert any(prompt in m["content"] for m in captured[-1]) is (opt_in and not service)
        if not opt_in:
            assert captured[-1] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_configured_service_tools_off_and_prompt_absent(configured, monkeypatch):
    stream = AsyncMock(return_value="answer")
    monkeypatch.setattr(execute, "_stream_and_collect", stream)
    model = AsyncMock()
    monkeypatch.setattr(execute, "_build_tool_model", model)
    result = await execute.execute_node(turn(agent_service_single_turn=True))
    assert result.get("error") is None
    assert stream.call_args.args[0] == [{"role": "user", "content": "hello"}]
    model.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("face_id", ["assistant", "planner", "researcher", "reviewer"])
@pytest.mark.parametrize("prompt_name", ["builder-prompt.txt", "quoted", "setup", "chat"])
async def test_selected_face_one_answer(configured, monkeypatch, face_id, prompt_name):
    if prompt_name == "builder-prompt.txt":
        task = (Path(__file__).parent / "fixtures/pa1-builder-prompt.txt").read_text(encoding="utf-8")
    else:
        task = {"quoted": 'Explain this quoted contract: "plan design implement review ' * 60,
                "setup": "Please set up a project and explain the available tools",
                "chat": "hello"}[prompt_name]
    planner = AsyncMock(side_effect=AssertionError("implicit decomposition"))
    monkeypatch.setattr(decompose, "_call_llm", planner)
    state = dict(turn(), task=task, face_id=face_id, selected_face_id=face_id)
    state.update(await route.route_node(state))
    assert state.get("error") is None and state["face_id"] == face_id
    assert state["chat_single_pass"] is True
    state.update(await decompose.decompose_node(state))
    # Dispatch must also ignore stale subtasks for an ordinary selected face.
    state["subtasks"] = ["one", "two", "three", "four", "five"]
    state.update(dispatch.dispatch_node(state))
    assert dispatch._route_after_dispatch(state) == "execute"
    reply = AsyncMock(return_value="one answer")
    monkeypatch.setattr(execute, "_stream_and_collect", reply)
    monkeypatch.setattr(execute, "_is_tool_enabled", lambda *args: False)
    result = await execute.execute_node(state)
    assert result["messages"] == [{"role": "assistant", "content": "one answer"}]
    planner.assert_not_awaited()
    reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"research_request": True}, {"hierarchical": True}, {"dispatch_subtask": {}},
    {"team": "explicit"}, {"fanout_width": 3},
])
async def test_explicit_work_can_fan_out(configured, monkeypatch, extra):
    monkeypatch.setattr(decompose, "_call_llm", AsyncMock(return_value=["one", "two", "three", "four", "five"]))
    monkeypatch.setattr(dispatch, "emit_event_sync", lambda *args: None)
    state = dict(turn(), task="Plan and review this explicit assignment", **extra)
    state.update(await decompose.decompose_node(state))
    state.update(dispatch.dispatch_node(state))
    sends = dispatch._route_after_dispatch(state)
    assert len(sends) == extra.get("fanout_width", 5) and all(s.node == "worker" for s in sends)


@pytest.mark.asyncio
async def test_pinning_preserves_requirements(configured, monkeypatch):
    for key, value, message in [
        ("backend_override", "minimax", "needs provider minimax"),
        ("team", "missing-team", "needs provider"),
        ("profile_name", "missing-profile", "needs provider/configuration profile"),
    ]:
        result = await route.route_node(dict(turn(pin_authoritative=True), **{key: value}))
        assert message in result["error"] and result["backend"] is None
    refusal = {"error": "flight budget exceeded", "flight_refused": True}
    monkeypatch.setattr("core.flight.enforcement.paused_flight_refusal", AsyncMock(return_value=refusal))
    assert await route.route_node(turn(pin_authoritative=True)) == refusal


def write_overlay(root, **changes):
    profile = dict(id="assistant", name="Lane Assistant", system_prompt="LANE_IDENTITY",
                   allowed_tools=["skill_view"], allowed_skills=[], skills=[])
    profile.update(changes)
    directory = root / "profiles"
    directory.mkdir(exist_ok=True)
    (directory / "assistant.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")


def test_overlay_precedence_and_isolation(configured, lane, monkeypatch):
    write_overlay(lane)
    registry = FaceRegistry()
    assert registry.get_system_prompt("assistant") == "LANE_IDENTITY"
    assert registry.get_face("reviewer").name == "Reviewer"
    assert registry.get_face("assistant").preferred_backend == configured.default_backend
    assert permissions.check_tool_access("assistant", "skill_view")
    assert not permissions.check_tool_access("assistant", "create_project")
    other = lane / "second-lane"
    other.mkdir()
    configure(other, "c")
    monkeypatch.setenv("BOB_LANE_ROOT", str(other))
    assert get_default_registry().get_system_prompt("assistant") != "LANE_IDENTITY"
    assert permissions.check_tool_access("assistant", "create_project")


@pytest.mark.parametrize("changes,match", [
    ({"allowed_tools": ["workspace.exec"]}, "workspace.exec"),
    ({"allowed_tools": ["mcp__imaginary__read"]}, "mcp__imaginary__read"),
    ({"skills": [dict(id="bob.bad", version=1, summary="bad prerequisites",
                     required_tools=["create_team"], operations=[], body="Do nothing.")]}, "create_team"),
])
def test_invalid_overlay_fails_closed_repeatedly(configured, lane, changes, match):
    write_overlay(lane, **changes)
    # Construct before refresh to prove no cached partial/default policy survives.
    registry = object.__new__(FaceRegistry)
    registry._dir = Path(__file__).parents[1] / "core/faces/templates"
    registry._faces, registry._lane_signature, registry._lane_managed = {}, None, True
    for _ in range(2):
        with pytest.raises(lc.LaneConfigurationError, match=match) as err:
            registry.get_face("assistant")
        assert "assistant" in str(err.value)
        assert registry._faces == {}


def test_setup_skill_and_no_builder(configured):
    registry = get_default_registry()
    assert set(registry.get_allowed_tools("assistant")) == EXPECTED_TOOLS
    token = _skill_face_id.set("assistant")
    try:
        skill = json.loads(skill_view.invoke({"skill_id": "bob.setup", "version": 1}))
        assert "bob.product" not in registry.get_face("assistant").allowed_skills
        timestamp, weekday = get_server_time.invoke({}).rsplit(" (", 1)
        now = datetime.fromisoformat(timestamp)
    finally:
        _skill_face_id.reset(token)
    assert skill["ok"] and skill["version"] == 1
    assert "routing map" in skill["body"] and "UNAPPLIED" in skill["body"]
    assert skill["body"] not in registry.get_system_prompt("assistant")
    assert "bob_guide" in registry.get_allowed_tools("assistant")
    assert now.tzinfo is not None
    assert weekday == now.strftime("%A") + ")"
    assert abs((datetime.now(timezone.utc) - now).total_seconds()) < 10


def test_existing_lane_reads_updated_shipped_assistant_after_restart(configured, monkeypatch, tmp_path):
    from core.roster import get_roster

    roster = get_roster()
    current = yaml.safe_load((roster.profiles_path / "assistant.yaml").read_text(encoding="utf-8"))
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    path = shipped / "assistant.yaml"
    previous = deepcopy(current)
    previous["allowed_tools"].remove("web_fetch")
    previous["system_prompt"] = "Previous shipped Assistant."
    path.write_text(yaml.safe_dump(previous), encoding="utf-8")
    monkeypatch.setattr(roster, "profiles_path", shipped)
    before_restart = FaceRegistry()
    assert "web_fetch" not in before_restart.get_allowed_tools("assistant")

    path.write_text(yaml.safe_dump(current), encoding="utf-8")
    assert "web_fetch" not in before_restart.get_allowed_tools("assistant")
    after_restart = FaceRegistry()
    assert "web_fetch" in after_restart.get_allowed_tools("assistant")
    assert after_restart.get_system_prompt("assistant") == current["system_prompt"].strip()

    # Explicit lane-owned profiles still replace shipped profiles; no silent grant.
    overlay = configured.root / "profiles"
    overlay.mkdir()
    (overlay / "assistant.yaml").write_text(yaml.safe_dump(previous), encoding="utf-8")
    overridden = FaceRegistry()
    assert "web_fetch" not in overridden.get_allowed_tools("assistant")
    assert overridden.get_system_prompt("assistant") == previous["system_prompt"]
    with pytest.raises(ValidationError):
        PipeCallArgs.model_validate({"op": "runs.start", "args": {"run_id": "fake"}})
    assert not {"agents.create", "agents.invoke", "workspace.exec", "runs.start"} & NATIVE_TOOLS.keys()


@pytest.mark.asyncio
async def test_ingress_records_selected_face(configured, monkeypatch):
    from api import server
    relay = AsyncMock(return_value="stream-response")
    monkeypatch.setattr(server, "_stream_graph_turn", relay)
    request = SimpleNamespace(
        app={server.GRAPH_KEY: object(), server.FACES_KEY: get_default_registry()},
        json=AsyncMock(return_value=dict(conversation_id="fixture", content="Plan a function",
                                        face_id="reviewer")))
    assert await server.chat(request) == "stream-response"
    state = relay.call_args.kwargs["graph_input"]
    assert state["selected_face_id"] == state["face_id"] == "reviewer"
    assert state["scope"] is None


@pytest.mark.asyncio
async def test_explicit_profiles_and_team_keep_topology(configured, lane, monkeypatch):
    from core.graph import _route_after_recall
    team_roles = {"worker": {"backend": "deepseek_v4_flash", "escalation_chain": []}}
    teams.create_team("workflow", team_roles)
    teams.create_profile("hierarchy", {"roles": team_roles, "hierarchical": True})
    teams.create_profile("panel", {
        "shape": "fusion", "seats": [{"posture": "worker", "backend": "deepseek_v4_flash"}],
        "synth_backend": "deepseek_v4_flash", "protocol_bounds": {"grounding": False}})
    for extra, expected in [
        ({"research_request": True}, "research_plan"),
        ({"hierarchical": True}, "manager_dispatch"),
        ({"profile_name": "hierarchy"}, "manager_dispatch"),
        ({"profile_name": "panel"}, "panel_dispatch"),
        ({"team": "workflow"}, "dispatch"),
    ]:
        state = dict(turn(), **extra)
        state.update(await route.route_node(state))
        assert state.get("error") is None, (extra, state)
        assert not state.get("chat_single_pass")
        assert _route_after_recall(state) == expected


@pytest.mark.asyncio
async def test_profile_provider_requirement_before_pinned_spend(configured, lane):
    directory = lane / "teams"
    directory.mkdir()
    (directory / "unavailable.yaml").write_text(yaml.safe_dump({
        "name": "unavailable", "roles": {"worker": {
            "backend": "minimax", "escalation_chain": []}}}), encoding="utf-8")
    out = await route.route_node(turn(profile_name="unavailable", pin_authoritative=True))
    assert "needs provider minimax" in out["error"]


@pytest.mark.asyncio
async def test_card_uses_bound_tools_after_permission_check(configured, monkeypatch):
    captured, bound = [], []
    class Transport:
        def bind_tools(self, tools):
            bound.extend(t.name for t in tools)
            return self
        async def ainvoke(self, messages):
            captured.extend(m.content for m in messages)
            return AIMessage(content="answer")
    monkeypatch.setattr(execute, "check_tool_access", lambda face, tool: tool == "skill_view")
    monkeypatch.setattr(execute, "_build_tool_model", lambda backend: Transport())
    result = await execute.execute_node(turn())
    assert result.get("error") is None
    assert bound == card_from(captured)["native_tools"] == ["skill_view"]
    assert card_from(captured)["chat_pipe_operations"] == []


def test_overlay_duplicate_face_ids_rejected(configured, lane):
    write_overlay(lane)
    (lane / "profiles/duplicate.yml").write_text(
        (lane / "profiles/assistant.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate face id: assistant"):
        FaceRegistry()


def test_legacy_ignores_lane_overlay(configured, lane, monkeypatch):
    write_overlay(lane, allowed_tools=["workspace.exec"])
    monkeypatch.setenv("BOB_ROSTER_FILE", str(Path(__file__).parents[2] / "bobclaw-core/tests/fixtures/deployment/roster.yaml"))
    assert FaceRegistry().get_face("assistant").name == "General Assistant"


@pytest.mark.asyncio
async def test_interactive_fanout_worker_prompt_and_internal_worker_exclusion(configured, monkeypatch):
    from core.nodes import worker
    monkeypatch.setattr(dispatch, "emit_event_sync", lambda *args: None)
    monkeypatch.setattr(worker, "_emit_worker_state", AsyncMock())
    send = AsyncMock(return_value="answer")
    monkeypatch.setattr(worker, "_send_to_backend", send)
    state = turn(selected_face_id="assistant")
    state["fanout_subtasks"] = [{"idx": 0, "text": "inspect"}, {"idx": 1, "text": "report"}]
    sends = dispatch._route_after_dispatch(state)
    prompt = get_default_registry().get_system_prompt("assistant")
    for branch in sends:
        assert branch.arg["interactive_face"] is True
        result = await worker.worker_node(branch.arg)
        assert result["worker_results"][0]["status"] == "ok"
        contents = [m["content"] for m in send.call_args.args[0]]
        assert contents[0] == prompt and sum(s.count(prompt) for s in contents) == 1
        assert card_from(contents)["native_tools"] == []
        assert prompt not in str(result)
    await worker.worker_node(dict(task="internal work", face_id="assistant", backend="deepseek_v4_flash"))
    assert send.call_args.args[0] == [{"role": "user", "content": "internal work"}]


def test_overlay_explicit_backend_and_default_inheritance(configured, lane):
    write_overlay(lane, preferred_backend="claude_api")
    registry = FaceRegistry()
    assert registry.get_face("assistant").preferred_backend == "claude_api"
    assert registry.get_face("reviewer").preferred_backend == configured.default_backend


def test_lane_registry_cache_key_includes_root(configured, lane, monkeypatch):
    from core.faces import registry as module
    # Simulate Python recycling a LaneConfiguration object's identity.
    monkeypatch.setattr(module, "id", lambda obj: 42, raising=False)
    monkeypatch.setattr(module, "_DEFAULT_REGISTRY", None)
    monkeypatch.setattr(module, "_DEFAULT_REGISTRY_FILE", None)
    write_overlay(lane)
    first = module.get_registry()
    assert first.get_system_prompt("assistant") == "LANE_IDENTITY"
    other = lane / "new-lane"
    other.mkdir()
    configure(other, "c", BOB_LANE_DEFAULT_BACKEND="deepseek_v4_flash")
    monkeypatch.setenv("BOB_LANE_ROOT", str(other))
    assert module.get_registry() is not first
    assert module.get_registry().get_system_prompt("assistant") != "LANE_IDENTITY"


@pytest.mark.asyncio
@pytest.mark.parametrize('face,service,expected', [('assistant', False, True), ('researcher', False, False), ('assistant', True, False)])
async def test_owner_starter_status_is_small_invocation_context(configured, monkeypatch, face, service, expected):
    captured = []
    read_starter = __import__('unittest.mock', fromlist=['Mock']).Mock(return_value=True)
    monkeypatch.setattr('core.starter_memory.starter_empty_for_owner', read_starter)
    monkeypatch.setattr(execute, '_is_tool_enabled', lambda *args: False)
    async def collect(messages, *args):
        captured.extend(messages)
        return 'answer'
    monkeypatch.setattr(execute, '_stream_and_collect', collect)
    result = await execute.execute_node(dict(turn(), face_id=face, user_id='owner', agent_service_single_turn=service))
    assert result.get('error') is None
    cards = [m['content'] for m in captured if m['content'].startswith('Owner starter headings empty:')]
    assert bool(cards) is expected
    assert read_starter.called is expected
    assert all(len(s) < 200 for s in cards)
    assert 'Owner starter headings' not in str(result)
