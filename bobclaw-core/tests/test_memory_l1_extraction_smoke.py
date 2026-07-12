"""
End-to-end smoke test for the L1 extraction pipeline.

Tests the full L0->L1->indexed->retrieved loop with live services.
Gated behind ``@pytest.mark.integration`` — NOT collected by ``pytest -q``.
Writes only to a unique throwaway Qdrant collection and proves teardown.

Run::

    pytest -m integration tests/test_memory_l1_extraction_smoke.py -v

Requires running:
- BoB Qdrant on MEMORY_QDRANT_URL (default http://localhost:6353 — NOT the LKS
  Qdrant on :6333; see tests/_memory_smoke_util.resolve_bob_qdrant_url).
- The embed_text slot server on :8081 (``granite-embedding-311m``) — real vectors
  are indexed and recalled.

The extract_small LLM is stubbed with a deterministic response so the L0->L1
dedup and recall ASSERTIONS are stable (the live gemma-4-e4b extractor is
non-deterministic in what it extracts). The R3 dedup identity itself is proven
deterministically in tests/memory/test_extractor_dedup_identity.py, and the fully
live extractor is exercised by the operator live-gate smoke.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from core.graph import AgentState, build_graph
from core.memory._hashing import verify_event_hash
from tests._memory_smoke_util import (
    assert_untouched,
    drop_collection,
    require_qdrant,
    resolve_bob_qdrant_url,
    snapshot_non_throwaway,
    throwaway_prefix,
    write_throwaway_stores_toml,
)

pytestmark = pytest.mark.integration

# Live embed_text slot endpoint (config/memory_slots.toml -> :8081, granite). The
# extract_small LLM is stubbed for determinism, so :8082 is not required here.
_EMBEDDER_URL = os.getenv("MEMORY_EMBEDDER_URL", "http://localhost:8081")
_DEFAULT_STORE_ID = "bobclaw_default"


def _build_invoke_state(task: str) -> AgentState:
    return {
        "messages": [],
        "task": task,
        "face_id": "assistant",
        "model_override": None,
        "backend": "local",
        "tools_allowed": ["code", "files"],
        "approval_required": False,
        "approval_response": None,
        "artifacts": [],
        "error": None,
        "subtasks": None,
        "phase": None,
        "dispatch_subtask": None,
        "worker_results": [],
        "fanout_subtasks": None,
        "fanout_width": None,
        "escalation_backend": None,
        "workspace_dir": None,
        "fanout_wave": None,
        "recalled_facts": None,
    }


def _check_http(url: str, path: str = "/healthz") -> None:
    """Check an HTTP endpoint is reachable; pytest.skip otherwise."""
    import http.client

    host_port = url.replace("http://", "").replace("https://", "")
    host = host_port.split(":")[0]
    port = int(host_port.split(":")[1]) if ":" in host_port else 80
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status not in (200, 404):
            pytest.skip(f"{url}{path} returned status {resp.status}")
        resp.read()
        conn.close()
    except Exception as exc:
        pytest.skip(f"{url} not reachable: {exc}")


@pytest.fixture(autouse=True)
def _reset_memory_bootstrap():
    from core.memory import bootstrap as _b
    _b._bootstrap_singleton = None
    _b._bootstrap_config_snapshot = None


@pytest.fixture(autouse=True)
def _check_extractor_module():
    if importlib.util.find_spec("core.memory.extractor") is None:
        pytest.skip("core.memory.extractor not available (W-INT-2C not landed yet)")


@pytest.fixture
def throwaway_store(tmp_path):
    """A BoB-Qdrant (:6353) run confined to a unique throwaway collection, with a
    proven-untouched invariant on every other collection (teardown-safe)."""
    url = resolve_bob_qdrant_url()
    require_qdrant(url)
    prefix = throwaway_prefix()
    stores_toml = write_throwaway_stores_toml(tmp_path, prefix)
    before = snapshot_non_throwaway(url, prefix)
    try:
        yield {"url": url, "prefix": prefix, "stores_toml": stores_toml}
    finally:
        drop_collection(url, f"{prefix}_768")
        assert_untouched(url, prefix, before)


@pytest.fixture
def _check_services(throwaway_store):
    # Live deps: BoB Qdrant (via throwaway_store) + the embed slot server (:8081).
    # The extract_small LLM is stubbed for determinism (see stub_extractor_llm).
    _check_http(_EMBEDDER_URL, "/v1/models")


def _extractor_llm_response(*fact_texts: str) -> dict:
    """Shape a deterministic LMStudio chat reply carrying the given facts."""
    facts = [{"text": t} for t in fact_texts]
    return {"choices": [{"message": {"content": json.dumps({"facts": facts})}}]}


@pytest.fixture
def stub_extractor_llm(monkeypatch):
    """Return a helper that pins the extractor LLM to a deterministic reply.

    Keeps the whole pipeline live (real events, real input_hash, real Qdrant
    index + recall) while removing the one non-deterministic component."""
    from unittest.mock import AsyncMock

    def _pin(*fact_texts: str) -> None:
        monkeypatch.setattr(
            "core.backends.lmstudio.LMStudioClient.chat",
            AsyncMock(return_value=_extractor_llm_response(*fact_texts)),
        )

    return _pin


def _bootstrap_memory(sqlite_path: Path, throwaway: dict) -> object:
    from core.memory.bootstrap import MemoryBootstrapConfig, bootstrap_memory
    bcfg = MemoryBootstrapConfig(
        enabled=True,
        sqlite_path=sqlite_path,
        qdrant_url=throwaway["url"],
        stores_config_path=throwaway["stores_toml"],
        default_store_id=_DEFAULT_STORE_ID,
    )
    return bootstrap_memory(bcfg)


# ═══════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════


class TestL1ExtractionSmoke:

    @pytest.mark.asyncio
    async def test_l1_extraction_smoke_full_loop(
        self, _check_services, throwaway_store, stub_extractor_llm, tmp_path, monkeypatch,
    ):
        """Full L0->L1->indexed->retrieved loop across two turns."""
        monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
        monkeypatch.setattr(
            "core.config.config.MEMORY_L1_EXTRACTION_ENABLED", True, raising=False
        )
        stub_extractor_llm(
            "User works as a marine biologist studying octopus cognition at UCSB."
        )

        async def _fake_stream(messages, backend, model_override=None):
            yield "Mock response about marine biology."

        monkeypatch.setattr("core.nodes.execute._stream_to_backend", _fake_stream)

        mem = _bootstrap_memory(tmp_path / "bobclaw_memory.db", throwaway_store)
        graph = build_graph(checkpointer=MemorySaver())

        # ── Turn 1: seed biographical info ──
        state1 = _build_invoke_state(
            "I work as a marine biologist studying octopus cognition at UCSB."
        )
        result1 = await graph.ainvoke(
            state1, {"configurable": {"thread_id": "l1-smoke-1"}}
        )

        # L0: exactly one agent_turn event
        events = [e async for e in mem.event_log.replay()]
        agent_turns = [e for e in events if e.kind == "agent_turn"]
        assert len(agent_turns) == 1, (
            f"Expected 1 agent_turn, got {len(agent_turns)}"
        )
        turn1_event_id = agent_turns[0].event_id

        # L1: at least one Fact was extracted with the correct source_event_id
        await mem.drain_extraction_tasks()
        facts = await mem.fact_store.query({"source_event_id": turn1_event_id})
        assert len(facts) >= 1, (
            f"Expected at least 1 fact for {turn1_event_id}, got {len(facts)}"
        )

        # Facts are indexed in Qdrant (chunks exist in the retrieval provider)
        await mem.drain_extraction_tasks()
        indexed_count = 0
        for f in facts:
            chunk_ids = list(
                mem.indexer._provider.scroll_payload(
                    mem.indexer._store_id, {"source_fact_id": f.fact_id}
                )
            )
            indexed_count += len(chunk_ids)
        assert indexed_count >= 1, (
            f"No indexed chunks found for extracted facts"
        )

        # ── Turn 2: recall query ──
        state2 = _build_invoke_state("What's my job?")
        result2 = await graph.ainvoke(
            state2, {"configurable": {"thread_id": "l1-smoke-1"}}
        )

        # L0 now has TWO events
        events2 = [e async for e in mem.event_log.replay()]
        agent_turns2 = [e for e in events2 if e.kind == "agent_turn"]
        assert len(agent_turns2) == 2, (
            f"Expected 2 agent_turns, got {len(agent_turns2)}"
        )

        # L0 hash chain is valid
        prev_hash = None
        for ev in events2:
            assert verify_event_hash(ev, prev_hash), (
                f"Hash mismatch for {ev.event_id}"
            )
            prev_hash = ev.hash

        # recalled_facts from turn 2 includes the fact extracted on turn 1
        recalled = result2.get("recalled_facts")
        assert recalled, "recalled_facts must be non-empty on turn 2"
        recalled_event_ids = {
            f.source_event_id for f in recalled if f.source_event_id
        }
        assert turn1_event_id in recalled_event_ids, (
            f"Turn 1's event {turn1_event_id} not found in turn 2's recalled_facts "
            f"(got source_event_ids: {recalled_event_ids})"
        )

    @pytest.mark.asyncio
    async def test_l1_extraction_disabled_no_facts(
        self, _check_services, throwaway_store, tmp_path, monkeypatch,
    ):
        """With L1 extraction disabled, L0 events write but no L1 facts created."""
        monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
        monkeypatch.setattr(
            "core.config.config.MEMORY_L1_EXTRACTION_ENABLED", False, raising=False
        )

        async def _fake_stream(messages, backend, model_override=None):
            yield "OK."

        monkeypatch.setattr("core.nodes.execute._stream_to_backend", _fake_stream)

        mem = _bootstrap_memory(tmp_path / "bobclaw_memory_no_l1.db", throwaway_store)
        graph = build_graph(checkpointer=MemorySaver())

        state = _build_invoke_state("Hello world")
        await graph.ainvoke(state, {"configurable": {"thread_id": "l1-dis"}})

        # L0 event exists
        events = [e async for e in mem.event_log.replay()]
        agent_turns = [e for e in events if e.kind == "agent_turn"]
        assert len(agent_turns) == 1, (
            f"Expected 1 agent_turn, got {len(agent_turns)}"
        )

        # No L1 facts for this event
        facts = await mem.fact_store.query(
            {"source_event_id": agent_turns[0].event_id}
        )
        assert len(facts) == 0, (
            f"Expected 0 facts when extraction disabled, got {len(facts)}"
        )

    @pytest.mark.asyncio
    async def test_l1_extraction_dedup_across_turns(
        self, _check_services, throwaway_store, stub_extractor_llm, tmp_path, monkeypatch,
    ):
        """Same message across two turns produces 2 L0 events but deduplicated L1 facts."""
        monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
        monkeypatch.setattr(
            "core.config.config.MEMORY_L1_EXTRACTION_ENABLED", True, raising=False
        )
        stub_extractor_llm("User's favorite color is blue.")

        async def _fake_stream(messages, backend, model_override=None):
            yield "Mock response."

        monkeypatch.setattr("core.nodes.execute._stream_to_backend", _fake_stream)

        mem = _bootstrap_memory(tmp_path / "bobclaw_memory_dedup.db", throwaway_store)
        graph = build_graph(checkpointer=MemorySaver())

        msg = "My favorite color is blue."
        state = _build_invoke_state(msg)

        # Turn 1 — drain so turn 1's fact is COMMITTED before turn 2 extracts. This
        # removes the async-extraction race so the test isolates the R3 dedup key.
        result1 = await graph.ainvoke(
            state, {"configurable": {"thread_id": "l1-dedup"}}
        )
        await mem.drain_extraction_tasks()

        # Turn 2 — same message (fresh random turn_id ⇒ same canonical identity)
        result2 = await graph.ainvoke(
            state, {"configurable": {"thread_id": "l1-dedup"}}
        )

        # L0 has TWO distinct events
        events = [e async for e in mem.event_log.replay()]
        agent_turns = [e for e in events if e.kind == "agent_turn"]
        assert len(agent_turns) == 2, (
            f"Expected 2 agent_turns for two turns, got {len(agent_turns)}"
        )
        assert agent_turns[0].event_id != agent_turns[1].event_id, (
            "Two turns must produce different event_ids"
        )

        # L1 fact count does NOT double — dedup via input_hash
        await mem.drain_extraction_tasks()
        facts_turn1 = await mem.fact_store.query(
            {"source_event_id": agent_turns[0].event_id}
        )
        facts_turn2 = await mem.fact_store.query(
            {"source_event_id": agent_turns[1].event_id}
        )
        facts_all = await mem.fact_store.all_ids()

        # R3: the two turns differ ONLY in volatile provenance (a fresh random
        # turn_id per turn), so the canonical extraction identity is equal and the
        # whole-event dedup gate fires. Turn 2 must add ZERO new facts, and the
        # total must not grow past turn 1. (Pre-R3 this grew 2 -> 6.)
        assert len(facts_turn2) == 0, (
            f"Turn 2 (identical fact, new turn_id) created {len(facts_turn2)} new "
            f"facts — dedup identity leaked volatile turn metadata"
        )
        assert len(facts_all) == len(facts_turn1), (
            f"Fact count changed from {len(facts_turn1)} (turn 1) to "
            f"{len(facts_all)} (total) — expected exact dedup, no growth"
        )
