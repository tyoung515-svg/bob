from pathlib import Path

import pytest

from core.backends.claude_code import ClaudeCodeClient
from core.backends.codex_code import CodexCodeClient


_WORKSPACE = Path(__file__).resolve().parents[1]


class SeatSubstitution(RuntimeError):
    pass


def resolve_seat(role: str):
    if role == "manager":
        return ClaudeCodeClient(
            cwd=_WORKSPACE,
            posture={"model": "opus", "permission_mode": "plan"},
            timeout=30,
            conversation_id="v098-r0-manager",
        )
    if role == "worker":
        return CodexCodeClient(
            cwd=_WORKSPACE,
            posture={"model": "gpt-5.6-terra"},
            timeout=30,
            conversation_id="v098-r0-worker",
        )
    if role == "audit":
        return ClaudeCodeClient(
            cwd=_WORKSPACE,
            posture={"model": "opus", "permission_mode": "plan"},
            timeout=30,
            conversation_id="v098-r0-audit",
        )
    raise ValueError(f"Unknown R0 seat role: {role}")


def assert_exact_seat(client, expected_model: str) -> None:
    actual_model = client.posture["model"]
    if actual_model != expected_model:
        raise SeatSubstitution(
            f"Expected exact seat model {expected_model!r}, got {actual_model!r}"
        )


def test_manager_seat_is_opus_claude_code():
    manager = resolve_seat("manager")

    assert isinstance(manager, ClaudeCodeClient)
    assert_exact_seat(manager, "opus")


def test_worker_seat_is_gpt56_terra_codex_code():
    worker = resolve_seat("worker")

    assert isinstance(worker, CodexCodeClient)
    assert_exact_seat(worker, "gpt-5.6-terra")


def test_audit_seat_is_distinct_claude_code_client_without_resume():
    manager = resolve_seat("manager")
    audit = resolve_seat("audit")

    assert isinstance(audit, ClaudeCodeClient)
    assert audit is not manager
    assert audit.conversation_id == "v098-r0-audit"
    assert audit.conversation_id != manager.conversation_id
    assert_exact_seat(audit, "opus")


def test_seat_substitution_fails_closed():
    substituted_worker = CodexCodeClient(
        cwd=_WORKSPACE,
        posture={"model": "gpt-5.6-luna"},
        timeout=30,
        conversation_id="v098-r0-worker",
    )

    with pytest.raises(SeatSubstitution):
        assert_exact_seat(substituted_worker, "gpt-5.6-terra")
