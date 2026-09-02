"""bobclaw-telegram — session_map tests (SQLite in tmp_path, hermetic)."""
from __future__ import annotations

from bobclaw_telegram.session_map import SessionMap


def _map(tmp_path):
    return SessionMap(tmp_path / ".data" / "sessions.db")


# ── chat_id → conversation mapping ────────────────────────────────────────

def test_unknown_chat_has_no_conversation(tmp_path):
    assert _map(tmp_path).conversation_for(42) is None


def test_ensure_conversation_stores_and_reads_back(tmp_path):
    sm = _map(tmp_path)
    assert sm.ensure_conversation(42, "conv-a") == "conv-a"
    assert sm.conversation_for(42) == "conv-a"


def test_ensure_conversation_first_mapping_wins(tmp_path):
    sm = _map(tmp_path)
    sm.ensure_conversation(42, "conv-a")
    # A concurrent/late create must not overwrite the existing mapping.
    assert sm.ensure_conversation(42, "conv-b") == "conv-a"
    assert sm.conversation_for(42) == "conv-a"


def test_mappings_are_per_chat(tmp_path):
    sm = _map(tmp_path)
    sm.ensure_conversation(1, "conv-1")
    sm.ensure_conversation(2, "conv-2")
    assert sm.conversation_for(1) == "conv-1"
    assert sm.conversation_for(2) == "conv-2"
    assert sm.conversation_for(3) is None


def test_mapping_survives_reopen(tmp_path):
    _map(tmp_path).ensure_conversation(42, "conv-a")
    assert _map(tmp_path).conversation_for(42) == "conv-a"


# ── update_id idempotency ────────────────────────────────────────────────

def test_last_update_id_defaults_to_zero(tmp_path):
    assert _map(tmp_path).last_update_id() == 0


def test_mark_processed_advances_watermark(tmp_path):
    sm = _map(tmp_path)
    sm.mark_processed(100)
    assert sm.last_update_id() == 100
    assert sm.is_replay(100)
    assert sm.is_replay(99)
    assert not sm.is_replay(101)


def test_watermark_is_monotonic(tmp_path):
    sm = _map(tmp_path)
    sm.mark_processed(100)
    sm.mark_processed(90)  # out-of-order/replayed id must not regress it
    assert sm.last_update_id() == 100


def test_watermark_survives_reopen(tmp_path):
    _map(tmp_path).mark_processed(55)
    sm = _map(tmp_path)
    assert sm.last_update_id() == 55
    assert sm.is_replay(55)  # replayed update after restart is skipped
