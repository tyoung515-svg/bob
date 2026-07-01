"""
BoBClaw Core — Unit tests for the opt-in LangChain tool-calling loop.

All model I/O is mocked; no network calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from core.nodes.execute import execute_node


# ─── Helpers ──────────────────────────────────────────────────────────────────

class _FakeBoundModel:
    """Mock LangChain model returned by ``FakeModel.bind_tools(...)``.

    Configurable via ``responses``: a list of AIMessage objects returned
    sequentially by ``ainvoke``. The last response is repeated once exhausted.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.invocations: list = []

    async def ainvoke(self, messages):
        self.invocations.append(messages)
        if len(self.invocations) <= len(self.responses):
            return self.responses[len(self.invocations) - 1]
        return self.responses[-1]


class _FakeModel:
    """Mock ChatOpenAI-like object returned by ``_build_tool_model``."""

    def __init__(self, responses):
        self._responses = responses
        self.bound_tools = None
        self.bound_model: "_FakeBoundModel | None" = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        self.bound_model = _FakeBoundModel(self._responses)
        return self.bound_model


def _tool_call_response(tool_name: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": tool_name,
                "args": {},
                "id": call_id,
            }
        ],
    )


def _final_response(text: str) -> AIMessage:
    return AIMessage(content=text)


@pytest.fixture
def _patch_event_log(monkeypatch):
    """Prevent the L0 event log from touching SQLite during tests."""
    monkeypatch.setattr(
        "core.nodes.execute._append_agent_turn_event",
        AsyncMock(return_value=None),
    )


# ─── Tool-loop behavior ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_loop_executes_tool_and_returns_final_answer(monkeypatch, _patch_event_log):
    """Model emits one tool_call → loop runs the tool → model returns final text."""
    final_text = "The current UTC time has been retrieved."
    fake_model = _FakeModel(
        responses=[
            _tool_call_response("get_server_time", "call_1"),
            _final_response(final_text),
        ]
    )
    monkeypatch.setattr("core.nodes.execute._build_tool_model", lambda _backend: fake_model)

    result = await execute_node(
        {
            "task": "What time is it?",
            "backend": "deepseek_v4_flash",
            "face_id": "assistant-tools",
            "messages": [],
            "approval_response": None,
        }
    )

    assert fake_model.bound_tools is not None
    assert any(t.name == "get_server_time" for t in fake_model.bound_tools)

    assert result.get("error") is None
    assert result["messages"][-1]["content"] == final_text


@pytest.mark.asyncio
async def test_tool_loop_runs_on_glm_5_2(monkeypatch, _patch_event_log):
    """GLM-5.2 is registered as tool-capable and can execute native tools."""
    final_text = "The current UTC time has been retrieved."
    fake_model = _FakeModel(
        responses=[
            _tool_call_response("get_server_time", "call_1"),
            _final_response(final_text),
        ]
    )
    monkeypatch.setattr("core.nodes.execute._build_tool_model", lambda _backend: fake_model)

    result = await execute_node(
        {
            "task": "What time is it?",
            "backend": "glm_5_2",
            "face_id": "assistant-tools",
            "messages": [],
            "approval_response": None,
        }
    )

    assert fake_model.bound_tools is not None
    assert any(t.name == "get_server_time" for t in fake_model.bound_tools)
    assert result.get("error") is None
    assert result["messages"][-1]["content"] == final_text


@pytest.mark.asyncio
async def test_tool_loop_respects_iteration_cap(monkeypatch, _patch_event_log):
    """A model that keeps emitting tool_calls must not loop forever."""
    fake_model = _FakeModel(
        responses=[_tool_call_response("get_server_time", f"call_{i}") for i in range(10)]
    )
    monkeypatch.setattr("core.nodes.execute._build_tool_model", lambda _backend: fake_model)

    result = await execute_node(
        {
            "task": "Keep checking the time",
            "backend": "deepseek_v4_flash",
            "face_id": "assistant-tools",
            "messages": [],
            "approval_response": None,
        }
    )

    # The loop is hard-capped at 5 iterations.
    assert fake_model.bound_model is not None
    assert len(fake_model.bound_model.invocations) == 5
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_check_tool_access_filters_disallowed_tools(monkeypatch, _patch_event_log):
    """check_tool_access is used to drop a registered tool the face cannot call."""
    captured_tools: list = []

    class _CaptureModel(_FakeModel):
        def bind_tools(self, tools):
            captured_tools.extend(tools)
            return super().bind_tools(tools)

    @tool
    def disallowed_tool() -> str:
        """A registered native tool that the face is not allowed to use."""
        return "nope"

    # Inject a second native tool and put both IDs on the face allowlist.
    # Then monkeypatch check_tool_access to deny the second one; the bound
    # set must contain only the permitted tool.
    from core.tools import registry as reg_mod

    reg_mod.NATIVE_TOOLS["disallowed_tool"] = disallowed_tool

    final_text = "Done."
    fake_model = _CaptureModel(
        responses=[
            _tool_call_response("get_server_time", "call_1"),
            _final_response(final_text),
        ]
    )
    monkeypatch.setattr("core.nodes.execute._build_tool_model", lambda _backend: fake_model)

    from core.faces.registry import get_default_registry
    from core.permissions import _FACE_TOOL_CACHE

    registry = get_default_registry()
    face = registry.get_face("assistant-tools")
    original_allowed = list(face.allowed_tools)
    face.allowed_tools = ["get_server_time", "disallowed_tool"]
    _FACE_TOOL_CACHE.clear()

    original_check = __import__("core.nodes.execute", fromlist=["check_tool_access"]).check_tool_access

    def _deny_disallowed(face_id: str, tool_name: str) -> bool:
        return tool_name == "get_server_time"

    monkeypatch.setattr("core.nodes.execute.check_tool_access", _deny_disallowed)

    try:
        await execute_node(
            {
                "task": "What time is it?",
                "backend": "deepseek_v4_flash",
                "face_id": "assistant-tools",
                "messages": [],
                "approval_response": None,
            }
        )
    finally:
        face.allowed_tools = original_allowed
        _FACE_TOOL_CACHE.clear()
        monkeypatch.setattr("core.nodes.execute.check_tool_access", original_check)
        reg_mod.NATIVE_TOOLS.pop("disallowed_tool", None)

    assert len(captured_tools) == 1
    assert captured_tools[0].name == "get_server_time"


@pytest.mark.asyncio
async def test_non_tool_face_path_unchanged(monkeypatch, _patch_event_log):
    """A normal deepseek_v4_flash turn on a non-tool face stays byte-identical."""
    captured_calls: list = []

    async def _fake_stream(messages, backend, model_override=None):
        captured_calls.append((list(messages), backend, model_override))
        for token in ("Hello", " world"):
            yield token

    monkeypatch.setattr("core.nodes.execute._stream_to_backend", _fake_stream)
    monkeypatch.setattr(
        "core.nodes.execute._check_escalation_pin",
        AsyncMock(return_value=None),
    )

    state = {
        "task": "Say hello",
        "backend": "deepseek_v4_flash",
        "face_id": "assistant",
        "messages": [],
        "approval_response": None,
        "model_override": None,
    }
    result = await execute_node(state)

    assert captured_calls, "normal path should call _stream_to_backend"
    messages, backend, model_override = captured_calls[0]
    assert backend == "deepseek_v4_flash"
    assert model_override is None
    # The user task is appended to the local message list.
    assert messages[-1] == {"role": "user", "content": "Say hello"}

    assert result.get("error") is None
    assert result["messages"][-1]["content"] == "Hello world"


# ─── Manual-dispatch error paths (P1) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_loop_reports_unknown_tool_call(monkeypatch, _patch_event_log):
    """A tool_call for a tool that is not bound must surface an error, not crash."""
    final_text = "Done."
    fake_model = _FakeModel(
        responses=[
            _tool_call_response("not_bound_tool", "call_1"),
            _final_response(final_text),
        ]
    )
    monkeypatch.setattr("core.nodes.execute._build_tool_model", lambda _backend: fake_model)

    class _StubTool:
        name = "get_server_time"

        async def ainvoke(self, _args):
            return "ok"

    monkeypatch.setattr(
        "core.nodes.execute.get_all_tools", AsyncMock(return_value=[_StubTool()])
    )

    result = await execute_node(
        {
            "task": "Try a bad tool",
            "backend": "deepseek_v4_flash",
            "face_id": "assistant-tools",
            "messages": [],
            "approval_response": None,
        }
    )

    assert result.get("error") is None
    assert result["messages"][-1]["content"] == final_text
    # The error is surfaced to the model as a ToolMessage on the re-call.
    assert fake_model.bound_model is not None
    second_call_msgs = fake_model.bound_model.invocations[1]
    tool_msgs = [m for m in second_call_msgs if isinstance(m, ToolMessage)]
    assert tool_msgs, "expected a ToolMessage in the second model call"
    assert "not_bound_tool" in tool_msgs[-1].content


@pytest.mark.asyncio
async def test_tool_loop_reports_tool_execution_error(monkeypatch, _patch_event_log):
    """A bound tool that raises must be reported, and the loop must continue."""
    final_text = "Recovered."
    fake_model = _FakeModel(
        responses=[
            _tool_call_response("failing_tool", "call_1"),
            _final_response(final_text),
        ]
    )
    monkeypatch.setattr("core.nodes.execute._build_tool_model", lambda _backend: fake_model)

    class _FailingTool:
        name = "failing_tool"

        async def ainvoke(self, _args):
            raise RuntimeError("tool exploded")

    monkeypatch.setattr(
        "core.nodes.execute.get_all_tools", AsyncMock(return_value=[_FailingTool()])
    )

    result = await execute_node(
        {
            "task": "Run a failing tool",
            "backend": "deepseek_v4_flash",
            "face_id": "assistant-tools",
            "messages": [],
            "approval_response": None,
        }
    )

    assert result.get("error") is None
    assert result["messages"][-1]["content"] == final_text
    assert fake_model.bound_model is not None
    second_call_msgs = fake_model.bound_model.invocations[1]
    tool_msgs = [m for m in second_call_msgs if isinstance(m, ToolMessage)]
    assert tool_msgs, "expected a ToolMessage in the second model call"
    assert "Error executing 'failing_tool'" in tool_msgs[-1].content


# ─── Tool registry sanity ─────────────────────────────────────────────────────

from core.tools.registry import NATIVE_TOOLS, get_tools


def test_get_tools_filters_by_allowed_list():
    assert len(get_tools(["get_server_time"])) == 1
    assert len(get_tools(["get_server_time", "not_a_tool"])) == 1
    assert len(get_tools(["not_a_tool"])) == 0


def test_get_server_time_returns_iso_utc():
    tool = NATIVE_TOOLS["get_server_time"]
    result = tool.invoke({})
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None
    assert parsed.tzinfo.utcoffset(parsed).total_seconds() == 0
