from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.cc_sessions import _lookup_cc_session
from core.config import config
from core.nodes import execute as execute_module


class _FakeClaudeCodeClient:
    def __init__(self, *, text: str, session_id: str) -> None:
        self.last_session_id = session_id
        self.chat = AsyncMock(return_value={"text": text, "session_id": session_id})


@pytest.fixture
def cc_session_paths(tmp_path: Path):
    original_sqlite = config.MEMORY_SQLITE_PATH
    original_sidecar = config.CC_SIDECAR_PATH
    original_project_dir = config.CC_PROJECT_DIR

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config.MEMORY_SQLITE_PATH = str(tmp_path / "cc_sessions.db")
    config.CC_SIDECAR_PATH = str(tmp_path / "cc_session_sidecar.jsonl")
    config.CC_PROJECT_DIR = str(project_dir)
    try:
        yield {
            "db": Path(config.MEMORY_SQLITE_PATH),
            "sidecar": Path(config.CC_SIDECAR_PATH),
            "project_dir": str(project_dir),
        }
    finally:
        config.MEMORY_SQLITE_PATH = original_sqlite
        config.CC_SIDECAR_PATH = original_sidecar
        config.CC_PROJECT_DIR = original_project_dir


def _state(conversation_id: str, task: str = "plan the rollout") -> dict:
    return {
        "task": task,
        "conversation_id": conversation_id,
        "backend": "claude_code",
        "messages": [],
        "cc_posture": {"permission_mode": "plan"},
        "escalation_backend": "claude_api",
    }


async def _run_cc_turn(client: _FakeClaudeCodeClient, state: dict) -> dict:
    with patch("core.backends.claude_code.ClaudeCodeClient", return_value=client):
        with patch.object(
            execute_module,
            "_append_agent_turn_event",
            AsyncMock(),
        ):
            return await execute_module.execute_node(state)


@pytest.mark.asyncio
async def test_first_turn_records_session_without_resume(cc_session_paths):
    client = _FakeClaudeCodeClient(text="first plan", session_id="sess-1")

    result = await _run_cc_turn(client, _state("conv-1"))

    _, kwargs = client.chat.await_args
    assert kwargs["resume_session_id"] is None
    assert result["cc_resume_session_id"] == "sess-1"
    assert await _lookup_cc_session("conv-1") == "sess-1"


@pytest.mark.asyncio
async def test_second_turn_resumes_stored_session(cc_session_paths):
    first = _FakeClaudeCodeClient(text="first plan", session_id="sess-1")
    second = _FakeClaudeCodeClient(text="second plan", session_id="sess-2")

    await _run_cc_turn(first, _state("conv-1"))
    result = await _run_cc_turn(second, _state("conv-1", task="continue it"))

    _, kwargs = second.chat.await_args
    assert kwargs["resume_session_id"] == "sess-1"
    assert result["cc_resume_session_id"] == "sess-2"
    assert await _lookup_cc_session("conv-1") == "sess-2"


@pytest.mark.asyncio
async def test_distinct_conversations_keep_distinct_sessions(cc_session_paths):
    first = _FakeClaudeCodeClient(text="a", session_id="sess-a")
    second = _FakeClaudeCodeClient(text="b", session_id="sess-b")

    await _run_cc_turn(first, _state("conv-a"))
    await _run_cc_turn(second, _state("conv-b"))

    assert await _lookup_cc_session("conv-a") == "sess-a"
    assert await _lookup_cc_session("conv-b") == "sess-b"


@pytest.mark.asyncio
async def test_sidecar_gets_one_well_formed_line_per_turn(cc_session_paths):
    first = _FakeClaudeCodeClient(text="a", session_id="sess-a")
    second = _FakeClaudeCodeClient(text="b", session_id="sess-b")

    await _run_cc_turn(first, _state("conv-a"))
    await _run_cc_turn(second, _state("conv-b"))

    lines = cc_session_paths["sidecar"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records == [
        {
            "conversation_id": "conv-a",
            "project_dir": cc_session_paths["project_dir"],
            "session_id": "sess-a",
            "ts": records[0]["ts"],
        },
        {
            "conversation_id": "conv-b",
            "project_dir": cc_session_paths["project_dir"],
            "session_id": "sess-b",
            "ts": records[1]["ts"],
        },
    ]
    for record in records:
        assert datetime.fromisoformat(record["ts"])
