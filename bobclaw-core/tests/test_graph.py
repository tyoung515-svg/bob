"""
BoBClaw Core — Unit tests for the LangGraph orchestration engine

All LLM / HTTP calls are mocked.  No running backends required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from core.graph import AgentState, _route_from_execute, build_graph
from core.nodes.approval import approval_node
from core.nodes.decompose import _is_complex, decompose_node
from core.nodes.execute import execute_node
from core.nodes.route import route_node
from core.permissions import check_tool_access, requires_approval, task_requires_approval


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_state(**overrides) -> AgentState:
    base: AgentState = {
        "messages": [],
        "task": "Say hello",
        "face_id": "builder-bob",
        "model_override": None,
        "backend": "local",
        "tools_allowed": ["code", "files"],
        "approval_required": False,
        "approval_response": None,
        "artifacts": [],
        "error": None,
    }
    base.update(overrides)
    return base


# ─── Graph compilation ────────────────────────────────────────────────────────

def test_graph_compilation_succeeds():
    graph = build_graph(checkpointer=MemorySaver())
    assert graph is not None


def test_graph_has_expected_nodes():
    graph = build_graph()
    node_names = set(graph.get_graph().nodes.keys())
    assert {"decompose", "route", "execute", "approval"}.issubset(node_names)


def test_route_from_execute_to_approval_when_flagged():
    state = make_state(approval_required=True, approval_response=None)
    assert _route_from_execute(state) == "approval"


def test_route_from_execute_to_end_when_not_flagged():
    from langgraph.graph import END
    state = make_state(approval_required=False)
    assert _route_from_execute(state) == END


def test_route_from_execute_to_end_when_already_responded():
    from langgraph.graph import END
    state = make_state(approval_required=True, approval_response="approved")
    assert _route_from_execute(state) == END


# ─── decompose_node ────────────────────────────────────────────────────────────

def test_is_complex_short_task():
    assert not _is_complex("Say hello")


def test_is_complex_long_task():
    assert _is_complex("x" * 200)


def test_is_complex_keyword_task():
    assert _is_complex("Implement a REST API for user auth")


@pytest.mark.asyncio
async def test_decompose_simple_task_passes_through():
    state = make_state(task="Say hello")
    result = await decompose_node(state)
    assert result["messages"]
    assert "Simple task" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_decompose_complex_task_calls_llm(monkeypatch):
    mock_subtasks = ["Set up database", "Create API routes", "Write tests"]

    async def fake_call_llm(task, backend):
        return mock_subtasks

    monkeypatch.setattr("core.nodes.decompose._call_llm", fake_call_llm)

    state = make_state(task="Implement a full REST API with authentication and tests")
    result = await decompose_node(state)

    assert result["messages"]
    content = result["messages"][0]["content"]
    assert "3 subtask" in content
    assert "Set up database" in content
    assert result.get("subtasks") == mock_subtasks


@pytest.mark.asyncio
async def test_decompose_llm_returns_single_item_on_failure(monkeypatch):
    async def fake_call_llm(task, backend):
        return [task]

    monkeypatch.setattr("core.nodes.decompose._call_llm", fake_call_llm)

    long_task = "Design and build a complete microservices architecture " * 3
    result = await decompose_node(state=make_state(task=long_task))
    assert result["messages"]


# ─── route_node ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_uses_model_override():
    state = make_state(model_override="claude_api", face_id="assistant")
    result = await route_node(state)
    assert result["backend"] == "claude_api"


@pytest.mark.asyncio
async def test_route_selects_local_when_available(monkeypatch):
    from core.backends.local_router import LocalBackendInfo

    mock_backends = [
        LocalBackendInfo("ollama", "http://localhost:11434", ["gemma-4-27b"]),
    ]
    mock_discover = AsyncMock(return_value=mock_backends)
    monkeypatch.setattr("core.nodes.route._router.discover", mock_discover)

    state = make_state(face_id="builder-bob", model_override=None)
    result = await route_node(state)
    assert result["backend"] == "ollama"


@pytest.mark.asyncio
async def test_route_falls_back_to_cloud_when_no_local(monkeypatch):
    mock_discover = AsyncMock(return_value=[])
    monkeypatch.setattr("core.nodes.route._router.discover", mock_discover)

    state = make_state(face_id="builder-bob", model_override=None)
    result = await route_node(state)
    # builder-bob escalates to minimax (senior tier; claude_managed was never implemented)
    assert result["backend"] == "minimax"


@pytest.mark.asyncio
async def test_route_respects_face_preferred_backend(monkeypatch):
    """researcher → prefers local; if local unavailable → gemini_deep_research"""
    mock_discover = AsyncMock(return_value=[])
    monkeypatch.setattr("core.nodes.route._router.discover", mock_discover)

    state = make_state(face_id="researcher", model_override=None)
    result = await route_node(state)
    assert result["backend"] == "gemini_deep_research"


@pytest.mark.asyncio
async def test_route_threads_planner_claude_cc_posture():
    state = make_state(face_id="planner-claude", model_override=None)
    result = await route_node(state)
    assert result["backend"] == "claude_code"
    assert result["escalation_backend"] == "claude_api"
    assert result["cc_posture"] == {
        "mode": "scratch_write",
        "permission_mode": "acceptEdits",
        "scratch_dir": "scratch",
    }


@pytest.mark.asyncio
async def test_route_lmstudio_preferred_on_windows_mock(monkeypatch):
    from core.backends.local_router import LocalBackendInfo

    ollama = LocalBackendInfo("ollama", "http://localhost:11434", ["model"])
    lmstudio = LocalBackendInfo("lmstudio", "http://localhost:1234", ["model"])

    mock_discover = AsyncMock(return_value=[ollama, lmstudio])
    monkeypatch.setattr("core.nodes.route._router.discover", mock_discover)
    # Platform preference logic is tested thoroughly in test_local_router.py;
    # here we just assert that one valid backend is chosen.
    # Use a still-local face (assistant now prefers deepseek_v4_flash).
    state = make_state(face_id="reviewer", model_override=None)
    result = await route_node(state)
    assert result["backend"] in ("ollama", "lmstudio")


# ─── execute_node ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_sets_approval_for_dangerous_task(monkeypatch):
    state = make_state(task="send email to boss@example.com about the budget")
    result = await execute_node(state)
    assert result.get("approval_required") is True


@pytest.mark.asyncio
async def test_execute_does_not_flag_safe_task(monkeypatch):
    async def fake_stream(messages, backend, model_override=None):
        yield "Hello, world!"

    monkeypatch.setattr("core.nodes.execute._stream_to_backend", fake_stream)

    state = make_state(task="Write hello world in Python")
    result = await execute_node(state)
    assert result.get("approval_required") is False
    assert result["messages"][0]["content"] == "Hello, world!"


@pytest.mark.asyncio
async def test_execute_proceeds_after_approval(monkeypatch):
    async def fake_stream(messages, backend, model_override=None):
        yield "Email sent!"

    monkeypatch.setattr("core.nodes.execute._stream_to_backend", fake_stream)

    state = make_state(
        task="send email to boss@example.com about the budget",
        approval_response="approved",
        approval_required=False,
    )
    result = await execute_node(state)
    assert result.get("approval_required") is False
    assert result["messages"][0]["content"] == "Email sent!"


@pytest.mark.asyncio
async def test_execute_stops_on_rejection():
    state = make_state(
        task="send email to boss@example.com",
        approval_response="rejected",
        approval_required=False,
    )
    result = await execute_node(state)
    assert "rejected" in result["messages"][0]["content"].lower()
    assert result.get("approval_required") is False


@pytest.mark.asyncio
async def test_execute_captures_backend_error(monkeypatch):
    async def failing_stream(messages, backend, model_override=None):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr("core.nodes.execute._stream_to_backend", failing_stream)

    state = make_state(task="Tell me a joke")
    result = await execute_node(state)
    assert result.get("error") == "connection refused"


# ─── approval_node ────────────────────────────────────────────────────────────

# The graph is compiled with ``interrupt_before=["approval"]`` so pausing is
# a checkpoint property, not a node-body concern.  ``approval_node`` therefore
# no longer calls ``langgraph.types.interrupt`` — it just translates the
# ``approval_response`` field (written by /api/chat/approval via
# ``graph.aupdate_state``) into the state flags execute_node reads.

def test_approval_gate_no_op_when_not_required():
    state = make_state(approval_required=False)
    result = approval_node(state)
    assert result == {}


def test_approval_gate_no_op_when_response_missing():
    """With interrupt_before pausing the graph, approval_node shouldn't run
    until /api/chat/approval has populated approval_response.  Guard against
    misconfiguration by treating a missing response as a no-op."""
    state = make_state(approval_required=True, approval_response=None)
    result = approval_node(state)
    assert result == {}


def test_approval_gate_processes_rejection():
    state = make_state(approval_required=True, approval_response="reject")
    result = approval_node(state)
    assert result["approval_response"] == "rejected"
    assert result["approval_required"] is False
    assert "rejected" in result["messages"][0]["content"].lower()


def test_approval_gate_processes_rejected_synonym():
    """Accept both ``reject`` and ``rejected`` as the rejecting decision."""
    state = make_state(approval_required=True, approval_response="rejected")
    result = approval_node(state)
    assert result["approval_response"] == "rejected"
    assert result["approval_required"] is False


def test_approval_gate_processes_approval():
    state = make_state(approval_required=True, approval_response="approve")
    result = approval_node(state)
    assert result["approval_required"] is False
    assert result["approval_response"] == "approve"


# ─── permissions ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("face_id,tool,expected", [
    # Fantasy capability labels no longer gate anything.
    ("builder-bob", "code",    False),
    ("builder-bob", "shell",   False),
    ("builder-bob", "email",   False),
    ("reviewer",    "email",   False),
    ("reviewer",    "code",    False),
    ("council-lite", "email",   False),
    ("researcher",  "search",  False),
    ("assistant",   "browser", False),
    ("assistant",   "shell",   False),
    # Real tool IDs still gate correctly.
    ("assistant-tools",   "get_server_time", True),
    ("assistant-actions", "create_project",  True),
    ("assistant-tools",   "create_project",  False),
])
def test_check_tool_access(face_id, tool, expected):
    assert check_tool_access(face_id, tool) is expected


@pytest.mark.parametrize("action_type,expected", [
    ("email_send",      True),
    ("email_reply",     True),
    ("form_submit",     True),
    ("purchase",        True),
    ("file_delete",     True),
    ("shell_dangerous", True),
    ("search",          False),
    ("code",            False),
    ("docs",            False),
    ("browser",         False),
])
def test_requires_approval(action_type, expected):
    assert requires_approval(action_type) is expected


@pytest.mark.parametrize("text,expected", [
    ("send email to alice@example.com", True),
    ("submit form with user data",      True),
    ("purchase 10 items",               True),
    ("rm -rf /tmp/old_data",            True),
    ("write a unit test",               False),
    ("explain how OAuth works",         False),
])
def test_task_requires_approval(text, expected):
    assert task_requires_approval(text) is expected
