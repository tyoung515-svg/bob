"""
BoBClaw Core — Gate-audit writes on the fan-out join (GR-P3-finish).

``join_node`` records what the scope Gate auto-cleared (``approved_by='gate'``,
status approved — non-blocking) and what it flagged (status pending,
``approved_by`` NULL) into the ``approvals`` store. The asyncpg pool is mocked;
no live Postgres calls. Audit writes are FAIL-OPEN: a missing pool / user_id or
a write error must NEVER raise out of the join.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.join import join_node


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fake_pool():
    """Mock asyncpg pool whose ``execute`` records INSERT calls."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    return pool


def _state(results, *, user_id="user-1", conversation_id=None):
    return {
        "worker_results": results,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }


def _insert_calls(pool):
    """Return [(status, approved_by, details_dict), ...] for each INSERT."""
    calls = []
    for call in pool.execute.call_args_list:
        args = call.args
        # INSERT signature: (sql, conv_uuid, user_id, action_type, details_json, status, approved_by)
        action_type = args[3]
        details = json.loads(args[4])
        status = args[5]
        approved_by = args[6]
        calls.append((action_type, status, approved_by, details))
    return calls


# ─── auto → approved/gate; gate/human → pending/NULL ──────────────────────────

@pytest.mark.asyncio
async def test_auto_worker_writes_gate_cleared_row(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [
            {
                "idx": 0,
                "status": "ok",
                "content": "did it",
                "gate_destination": "auto",
                "gate_reasons": ["in scope"],
            }
        ]
    )
    result = await join_node(state)
    assert result.get("error") is None

    calls = _insert_calls(pool)
    assert len(calls) == 1
    action_type, status, approved_by, details = calls[0]
    assert action_type == "worker_scope_review"
    assert status == "approved"
    assert approved_by == "gate"
    assert details == {"subtask_idx": 0, "reasons": ["in scope"]}


@pytest.mark.asyncio
async def test_gate_worker_writes_pending_row(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [
            {
                "idx": 1,
                "status": "flagged",
                "content": "drifted",
                "gate_destination": "gate",
                "gate_reasons": ["minor drift"],
            }
        ]
    )
    await join_node(state)

    calls = _insert_calls(pool)
    assert len(calls) == 1
    action_type, status, approved_by, details = calls[0]
    assert status == "pending"
    assert approved_by is None
    assert details == {"subtask_idx": 1, "reasons": ["minor drift"]}


@pytest.mark.asyncio
async def test_human_worker_writes_pending_row(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [
            {
                "idx": 2,
                "status": "rejected",
                "gate_destination": "human",
                "gate_reasons": ["touches .secrets"],
            }
        ]
    )
    await join_node(state)

    calls = _insert_calls(pool)
    assert len(calls) == 1
    _, status, approved_by, _ = calls[0]
    assert status == "pending"
    assert approved_by is None


@pytest.mark.asyncio
async def test_mixed_workers_write_one_row_each(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [
            {"idx": 0, "status": "ok", "content": "a", "gate_destination": "auto", "gate_reasons": []},
            {"idx": 1, "status": "flagged", "content": "b", "gate_destination": "gate", "gate_reasons": ["drift"]},
            # A worker with no gate verdict must NOT produce an audit row.
            {"idx": 2, "status": "ok", "content": "c"},
        ]
    )
    await join_node(state)

    calls = _insert_calls(pool)
    assert len(calls) == 2
    statuses = sorted((status, approved_by) for _, status, approved_by, _ in calls)
    assert statuses == [("approved", "gate"), ("pending", None)]


@pytest.mark.asyncio
async def test_no_gate_destination_writes_nothing(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state([{"idx": 0, "status": "ok", "content": "plain"}])
    await join_node(state)
    pool.execute.assert_not_called()


# ─── Fail-open: missing identity / pool / write error never raises ────────────

@pytest.mark.asyncio
async def test_missing_user_id_is_fail_open(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [{"idx": 0, "status": "ok", "gate_destination": "auto", "gate_reasons": []}],
        user_id=None,
    )
    # Must not raise; no audit row written without an identity.
    result = await join_node(state)
    assert result.get("error") is None
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_uninitialised_pool_is_fail_open(monkeypatch):
    def _boom():
        raise RuntimeError("Postgres pool not initialised")

    monkeypatch.setattr("core.db.get_pool", _boom)

    state = _state(
        [{"idx": 0, "status": "ok", "gate_destination": "auto", "gate_reasons": []}]
    )
    # The get_pool() failure must be swallowed, not propagated.
    result = await join_node(state)
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_write_error_is_fail_open(monkeypatch):
    pool = AsyncMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("connection reset"))
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [
            {"idx": 0, "status": "ok", "gate_destination": "auto", "gate_reasons": []},
            {"idx": 1, "status": "flagged", "gate_destination": "gate", "gate_reasons": ["x"]},
        ]
    )
    # A failed audit write must NOT abort or error the turn; the join still
    # produces its assistant message. Both writes are attempted (per-row swallow).
    result = await join_node(state)
    assert result.get("error") is None
    assert result["messages"][0]["role"] == "assistant"
    assert pool.execute.call_count == 2


@pytest.mark.asyncio
async def test_conversation_id_cast_to_uuid(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    conv_id = "11111111-1111-1111-1111-111111111111"
    state = _state(
        [{"idx": 0, "status": "ok", "gate_destination": "auto", "gate_reasons": []}],
        conversation_id=conv_id,
    )
    await join_node(state)

    from uuid import UUID

    conv_arg = pool.execute.call_args.args[1]
    assert conv_arg == UUID(conv_id)


@pytest.mark.asyncio
async def test_bad_conversation_id_drops_to_null(monkeypatch):
    pool = _fake_pool()
    monkeypatch.setattr("core.db.get_pool", lambda: pool)

    state = _state(
        [{"idx": 0, "status": "ok", "gate_destination": "auto", "gate_reasons": []}],
        conversation_id="not-a-uuid",
    )
    await join_node(state)
    conv_arg = pool.execute.call_args.args[1]
    assert conv_arg is None
