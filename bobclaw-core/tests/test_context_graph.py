"""Message index (conv_messages) + the conv_expand / conv_search tools."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.context import store
from core.faces.registry import FaceRegistry
from core.tools.conv_tools import conv_expand, conv_search
from core.tools.memory_tools import memory_tool_conversation_id, memory_tool_user_id
from core.tools.registry import NATIVE_TOOLS

PROFILES_DIR = Path(__file__).resolve().parents[2] / "bobclaw-core" / "tests" / "fixtures" / "deployment" / "faces"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "ctx.db"))


def _as(user, conversation):
    """Bind the caller the way execute.py does around the tool loop (each test runs in its own context)."""
    memory_tool_user_id.set(user)
    memory_tool_conversation_id.set(conversation)


def _hist(n, prefix="m", size=30):
    return [
        {"id": f"{prefix}{i}", "role": "user" if i % 2 == 0 else "assistant",
         "content": f"{prefix}{i} text " + "x" * size, "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00"}
        for i in range(n)
    ]


# ── store ────────────────────────────────────────────────────────────────────

def test_record_history_indexes_in_order_with_parent_links_and_is_idempotent():
    assert store.record_history("c1", "dad", _hist(4)) == 4
    rows = store.list_messages("c1", "dad")
    assert [r["message_id"] for r in rows] == ["m0", "m1", "m2", "m3"]
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]
    assert json.loads(rows[0]["parents_json"]) == [] and json.loads(rows[2]["parents_json"]) == ["m1"]
    assert rows[1]["content_sha256"] == store.sha256_text(rows[1]["content"])
    assert store.record_history("c1", "dad", _hist(4)) == 0                      # idempotent
    assert store.record_history("c1", "dad", _hist(6)) == 2                      # only the new ones
    rows = store.list_messages("c1", "dad")
    assert [r["seq"] for r in rows] == list(range(6)) and json.loads(rows[4]["parents_json"]) == ["m3"]


def test_a_later_batch_chains_onto_the_last_stored_message():
    store.record_history("c1", "dad", _hist(3))
    store.record_history("c1", "dad", [{"id": "z9", "role": "user", "content": "new"}])
    rows = store.list_messages("c1", "dad")
    assert rows[-1]["message_id"] == "z9" and json.loads(rows[-1]["parents_json"]) == ["m2"]


def test_messages_without_ids_or_content_are_skipped():
    hist = [{"role": "user", "content": "no id"}, {"id": "x", "role": "user", "content": ""},
            {"id": "y", "role": "user", "content": "ok"}]
    assert store.record_history("c1", "dad", hist) == 1
    assert store.record_history(None, "dad", hist) == 0 and store.record_history("c1", "dad", []) == 0


def test_reads_are_scoped_to_conversation_and_owner():
    store.record_history("c1", "dad", _hist(3))
    store.record_history("c2", "dad", _hist(3, prefix="q"))
    got, missing = store.get_by_ids("c1", "dad", ["m1", "q1", "nope"])
    assert [r["message_id"] for r in got] == ["m1"] and missing == ["q1", "nope"]
    assert store.get_by_ids("c1", "luna", ["m1"])[0] == [] and store.list_messages("c1", "luna") == []


def test_the_context_db_sits_beside_the_lane_memory_db_unless_set_explicitly(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTEXT_DB", raising=False)
    monkeypatch.delenv("MEMORY_SQLITE_PATH", raising=False)
    assert store.db_path() == ".memory/bobclaw_context.db"
    lane = tmp_path / "family/luna/bob-memory"
    monkeypatch.setenv("MEMORY_SQLITE_PATH", str(lane / "bobclaw_memory.db"))
    assert store.db_path() == str(lane / "bobclaw_context.db")
    monkeypatch.setenv("CONTEXT_DB", "/tmp/elsewhere.db")
    assert store.db_path() == "/tmp/elsewhere.db"


def test_the_same_message_ids_in_two_conversations_are_all_indexed():
    assert store.record_history("c1", "dad", _hist(4)) == 4
    assert store.record_history("c2", "dad", _hist(3)) == 3                      # same ids m0..m2, other conversation
    assert [r["message_id"] for r in store.list_messages("c2", "dad")] == ["m0", "m1", "m2"]
    assert [r["seq"] for r in store.list_messages("c2", "dad")] == [0, 1, 2]
    assert len(store.list_messages("c1", "dad")) == 4
    assert store.get_by_ids("c2", "dad", ["m1"])[0][0]["conversation_id"] == "c2"


def test_two_owners_of_a_same_named_conversation_cannot_see_or_block_each_other():
    store.record_history("shared", "dad", _hist(3))
    luna = [{"id": f"m{i}", "role": "user", "content": f"luna says {i}"} for i in range(3)]
    assert store.record_history("shared", "luna", luna) == 3                     # same conversation id and message ids
    assert [r["content"] for r in store.list_messages("shared", "luna")] == ["luna says 0", "luna says 1", "luna says 2"]
    assert store.list_messages("shared", "dad")[0]["content"].startswith("m0 text")
    assert store.get_by_ids("shared", "luna", ["m1"])[0][0]["content"] == "luna says 1"
    assert [r["seq"] for r in store.list_messages("shared", "luna")] == [0, 1, 2]
    assert store.search("shared", "luna", "text") == [] and len(store.search("shared", "dad", "text")) == 3


def test_resolve_range_either_direction_and_unknown_end():
    store.record_history("c1", "dad", _hist(6))
    assert [r["message_id"] for r in store.resolve_range("c1", "dad", "m1", "m3")] == ["m1", "m2", "m3"]
    assert [r["message_id"] for r in store.resolve_range("c1", "dad", "m3", "m1")] == ["m1", "m2", "m3"]
    assert store.resolve_range("c1", "dad", "m1", "zzz") is None
    assert store.resolve_range("c1", "luna", "m1", "m3") is None


_SEARCH = [
    {"id": "a", "role": "user", "content": "Deploy the Alpha build to 100% of users"},
    {"id": "b", "role": "assistant", "content": "alpha build done; deploy_target is staging"},
    {"id": "c", "role": "user", "content": "unrelated"},
]


def test_search_needs_all_terms_ignores_case_treats_wildcards_literally_and_is_newest_first():
    store.record_history("c1", "dad", _SEARCH)
    hits = store.search("c1", "dad", "ALPHA deploy")
    assert [h["message_id"] for h in hits] == ["b", "a"]
    assert "Alpha build" in hits[1]["snippet"]
    assert store.search("c1", "dad", "100%")[0]["message_id"] == "a"
    assert store.search("c1", "dad", "deploy_target")[0]["message_id"] == "b"
    assert store.search("c1", "dad", "dep%y") == [] and store.search("c1", "dad", "   ") == []
    assert store.search("c1", "luna", "alpha") == []


# ── tools ────────────────────────────────────────────────────────────────────

async def test_conv_expand_single_range_and_mixed_ids():
    store.record_history("c1", "dad", _hist(8))
    _as("dad", "c1")
    out = await conv_expand.ainvoke({"ids": "m1"})
    assert "id=m1" in out and "role=assistant" in out and "m1 text" in out and "sha256=" in out
    out = await conv_expand.ainvoke({"ids": "m2..m4"})
    assert all(f"id=m{i}" in out for i in (2, 3, 4)) and "showing 1-3 of 3" in out
    out = await conv_expand.ainvoke({"ids": "m0, m3..m4 m0"})
    assert out.count("id=m0") == 1 and "id=m3" in out and "id=m4" in out


async def test_conv_expand_pages_long_results():
    store.record_history("c1", "dad", _hist(25))
    _as("dad", "c1")
    first = await conv_expand.ainvoke({"ids": "m0..m24"})
    assert "showing 1-20 of 25" in first and "next_offset=20" in first
    assert "id=m19" in first and "id=m20" not in first
    second = await conv_expand.ainvoke({"ids": "m0..m24", "offset": 20})
    assert "showing 21-25 of 25" in second and "id=m24" in second and "next_offset" not in second


async def test_conv_expand_truncates_one_huge_message_with_a_notice():
    store.record_history("c1", "dad", [{"id": "big", "role": "assistant", "content": "B" * 50_000}])
    _as("dad", "c1")
    out = await conv_expand.ainvoke({"ids": "big"})
    assert "30000 more characters of this message not shown" in out


async def test_conv_expand_reports_missing_and_never_crosses_owner_or_conversation():
    store.record_history("c1", "dad", _hist(3))
    store.record_history("c2", "dad", _hist(3, prefix="q"))
    _as("dad", "c1")
    out = await conv_expand.ainvoke({"ids": "m1 q1 nope"})
    assert "id=m1" in out and "not found: q1, nope" in out
    assert (await conv_expand.ainvoke({"ids": "q1"})).startswith("[error:not_found]")
    _as("luna", "c1")
    assert (await conv_expand.ainvoke({"ids": "m1"})).startswith("[error:not_found]")
    assert (await conv_expand.ainvoke({"ids": "m0..m2"})).startswith("[error:not_found]")


async def test_conv_tools_need_a_conversation_and_a_query():
    assert (await conv_expand.ainvoke({"ids": "m1"})).startswith("[error:no_conversation]")
    assert (await conv_search.ainvoke({"query": "x"})).startswith("[error:no_conversation]")
    _as("dad", "c1")
    assert (await conv_expand.ainvoke({"ids": "  "})).startswith("[error:empty]")
    assert (await conv_search.ainvoke({"query": " "})).startswith("[error:empty]")


async def test_conv_search_lists_hits_with_ids():
    store.record_history("c1", "dad", _SEARCH)
    _as("dad", "c1")
    out = await conv_search.ainvoke({"query": "alpha deploy", "limit": 5})
    assert out.startswith("conv_search: 2 hit(s)") and "id=b" in out and "id=a" in out
    assert (await conv_search.ainvoke({"query": "zzzz"})).startswith("conv_search: no earlier message contains all of")


# ── wiring ───────────────────────────────────────────────────────────────────

def test_tools_are_registered_and_only_on_faces_that_already_have_tools():
    registry = FaceRegistry(profiles_dir=PROFILES_DIR)
    pair = {"conv_expand", "conv_search"}
    assert pair <= set(NATIVE_TOOLS)
    for face in ("assistant", "assistant-tools", "discord-bob"):
        assert pair <= set(registry.get_allowed_tools(face))
    for face in ("reviewer", "researcher", "builder-bob", "worker-deepseek"):
        # Every face now has bob_guide; these roster faces still lack context access.
        assert pair.isdisjoint(registry.get_allowed_tools(face))
