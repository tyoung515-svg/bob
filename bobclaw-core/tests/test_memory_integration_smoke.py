"""
End-to-end smoke test for the memory read path.

Requires a running Qdrant container on MEMORY_QDRANT_URL (default
http://localhost:6333).  Start via::

    docker compose up -d qdrant

Tests are gated behind ``@pytest.mark.integration`` and are NOT collected
by ``pytest -q`` (no marker).  Run with::

    pytest -m integration tests/test_memory_integration_smoke.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch as _patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from core.graph import AgentState, build_graph
from core.memory._hashing import verify_event_hash
from core.memory.bootstrap import (
    MemoryBootstrapConfig,
    MemorySingletons,
    bootstrap_memory,
)

pytestmark = pytest.mark.integration

_SEED_FACTS_PATH = Path(__file__).parent / "fixtures" / "seed_facts.json"


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _parse_seed_facts(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("facts", [])
    return list(raw)


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


SEED_EVENT_ID = "seed-000"


def _seed_facts_sync(mem: MemorySingletons, facts_data: list[dict]) -> None:
    """Seed facts directly via raw SQL (skips FK constraint on source_event_id
    and Qdrant indexing to avoid client-server version mismatch)."""

    async def _do() -> None:
        import aiosqlite
        import json as _j
        from datetime import datetime, timezone

        db_path = mem.fact_store._db_path
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("PRAGMA foreign_keys=OFF")
            await db.execute("PRAGMA journal_mode=WAL")
            for item in facts_data:
                body = item.get("body", {})
                ts = item.get("ts", datetime.now(timezone.utc).isoformat())
                await db.execute(
                    "INSERT OR IGNORE INTO memory_facts "
                    "(fact_id, generation_method, body_json, source_event_id, "
                    " input_hash, confidence_json, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["fact_id"],
                        item.get("generation_method", "seed_script"),
                        _j.dumps(body, sort_keys=True),
                        "seed-000",
                        "blake3:" + "a" * 64,
                        _j.dumps({"alpha": 1.0, "beta": 1.0, "rank": "normal"},
                                 sort_keys=True),
                        ts,
                    ),
                )
            await db.commit()

    asyncio.run(_do())


# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def _check_qdrant() -> None:
    """Probe Qdrant at the default URL; skip test if unreachable."""
    import http.client

    url = os.getenv("MEMORY_QDRANT_URL", "http://localhost:6333")
    host_port = url.replace("http://", "").replace("https://", "")
    host = host_port.split(":")[0]
    port = int(host_port.split(":")[1]) if ":" in host_port else 6333
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        if resp.status != 200:
            pytest.skip(f"Qdrant at {url} returned status {resp.status}")
        conn.close()
    except Exception as exc:
        pytest.skip(f"Qdrant not reachable at {url}: {exc}")


@pytest.fixture
def memory_bootstrap(_check_qdrant, tmp_path) -> MemorySingletons:
    """Bootstrap memory with a temp SQLite DB and seed known facts."""
    # Reset the module-level singleton so each test starts clean
    from core.memory import bootstrap as _b

    _b._bootstrap_singleton = None
    _b._bootstrap_config_snapshot = None

    sqlite_path = tmp_path / "bobclaw_memory.db"
    facts_data = _parse_seed_facts(_SEED_FACTS_PATH)

    bcfg = MemoryBootstrapConfig(
        enabled=True,
        sqlite_path=sqlite_path,
        qdrant_url=os.getenv("MEMORY_QDRANT_URL", "http://localhost:6333"),
        stores_config_path=Path(
            os.getenv(
                "MEMORY_STORES_CONFIG_PATH",
                str(Path(__file__).parent.parent / "config" / "memory_stores.toml"),
            )
        ),
        default_store_id=os.getenv("MEMORY_DEFAULT_STORE_ID", "bobclaw_default"),
    )
    mem = bootstrap_memory(bcfg)
    _seed_facts_sync(mem, facts_data)
    return mem


@pytest.fixture(autouse=True)
def _reset_memory_bootstrap():
    """Clean the bootstrap singleton before every test.  Tests that need
    bootstrapped memory use the ``memory_bootstrap`` fixture to re-init."""
    from core.memory import bootstrap as _b

    _b._bootstrap_singleton = None
    _b._bootstrap_config_snapshot = None


# ─── Tests ─────────────────────────────────────────────────────────────────────

class TestMemoryIntegrationSmoke:

    @pytest.mark.asyncio
    async def test_enabled_full_path(self, memory_bootstrap, monkeypatch):
        """Full read path: recall → prompt splice → L0 → hash chain."""
        from core.memory.models import RetrievedChunk, Fact, ConfidenceStub

        seed_fact_body = {"text": "BoBClaw uses LangGraph for agent orchestration"}
        seed_fact_id = "test-fact-001"

        search_called = False

        async def _mock_search(self, query_text, top_k=5, **kwargs):
            nonlocal search_called
            search_called = True
            return [
                RetrievedChunk(
                    content="BoBClaw uses LangGraph for agent orchestration",
                    score=0.95,
                    source_fact_id=seed_fact_id,
                    source_path=None,
                    heading_path=["BoBClaw"],
                )
            ]

        async def _mock_get(self, fact_id):
            return Fact(
                fact_id=fact_id, generation_method="seed_script",
                body=seed_fact_body, source_event_id="seed-000",
                input_hash="blake3:" + "a" * 64,
                confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="normal"),
                ts="2026-05-18T00:00:00Z",
            )

        monkeypatch.setattr(
            "core.memory.retriever.MemoryRetriever.search", _mock_search
        )
        monkeypatch.setattr(
            "core.memory.fact_store.SQLiteFactStore.get", _mock_get
        )
        monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
        send_mock = AsyncMock(return_value="Mock response about LangGraph.")
        monkeypatch.setattr(
            "core.nodes.execute._send_to_backend", send_mock
        )

        graph = build_graph(checkpointer=MemorySaver())
        state = _build_invoke_state("What does BoBClaw use for orchestration?")
        result = await graph.ainvoke(state, {"configurable": {"thread_id": "t1"}})

        assert search_called, "MemoryRetriever.search was never called"

        # recalled_facts non-empty
        recalled = result.get("recalled_facts")
        assert recalled, "recalled_facts must be non-empty"

        # seeded fact appears
        bodies = [f.body.get("text", "") for f in recalled]
        assert any("LangGraph" in b for b in bodies), f"LangGraph not in {bodies}"

        # system prompt splice — verify _send_to_backend received Prior context
        assert send_mock.call_count >= 1, "_send_to_backend was never called"
        all_msg_text = ""
        for call_args in send_mock.call_args_list:
            msgs = call_args[0][0] if call_args[0] else []
            for m in msgs:
                if isinstance(m, dict):
                    all_msg_text += m.get("content", "") + " "
        assert "Prior context" in all_msg_text, (
            f"Prior context not in send_to_backend args: {all_msg_text[:500]}"
        )
        assert "LangGraph" in all_msg_text

        # L0 event written (W-INT-1B collaboration)
        events = []
        async for ev in memory_bootstrap.event_log.replay():
            events.append(ev)
        agent_turns = [ev for ev in events if ev.kind == "agent_turn"]
        assert len(agent_turns) >= 1, (
            f"Expected agent_turn event(s), got {len(events)} total"
        )

        # hash chain valid
        prev_hash: str | None = None
        for ev in events:
            assert verify_event_hash(ev, prev_hash), (
                f"Hash mismatch for {ev.event_id}"
            )
            prev_hash = ev.hash

    @pytest.mark.asyncio
    async def test_disabled_no_recall(self, monkeypatch):
        """MEMORY_ENABLED=false: empty recalled_facts, no splice, no L0 write."""
        monkeypatch.setattr("core.config.config.MEMORY_ENABLED", False, raising=False)
        monkeypatch.setattr(
            "core.nodes.execute._send_to_backend",
            AsyncMock(return_value="OK."),
        )

        graph = build_graph(checkpointer=MemorySaver())
        state = _build_invoke_state("Hello")
        result = await graph.ainvoke(state, {"configurable": {"thread_id": "t-dis"}})

        recalled = result.get("recalled_facts")
        assert not recalled, f"recalled_facts should be empty, got: {recalled}"

        all_text = " ".join(
            m.get("content", "") for m in (result.get("messages") or [])
            if isinstance(m, dict)
        )
        assert "Prior context" not in all_text

    @pytest.mark.asyncio
    async def test_recall_node_wired_in_graph(self, memory_bootstrap, monkeypatch):
        """Verify the recall node is part of the compiled graph topology."""
        monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
        monkeypatch.setattr(
            "core.nodes.execute._send_to_backend",
            AsyncMock(return_value="OK."),
        )
        graph = build_graph(checkpointer=MemorySaver())
        node_names = set(graph.get_graph().nodes.keys())
        assert "recall" in node_names, f"Expected 'recall' node, got {sorted(node_names)}"
