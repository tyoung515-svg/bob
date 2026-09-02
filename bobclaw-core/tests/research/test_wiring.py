"""Tests for the research-lane federated LKS wiring (core/research/wiring.py) — the hermes seam.

Covers the brief's test matrix:
* env parse (empty / one / many / whitespace / empty segments / dupes);
* spec/factory gating — empty env ⇒ byte-identical no-op, set env ⇒ a retriever bound to the
  configured instances;
* availability fail-OPEN (memory not bootstrapped / adapter build failure ⇒ no spec, no cache
  poisoning) vs fingerprint/ACL fail-CLOSED (the retriever propagates);
* the shared adapter builder's per-instance pre-flight (unknown/unstamped dropped, stamped kept);
* the dispatch edge gate (research_tier + env ⇒ research_subagent Sends; either absent ⇒ plain);
* end-to-end through run_iterresearch: evidence living ONLY in a hermes instance surfaces in the
  condensed return with ``lks:hermes:*`` provenance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from langgraph.types import Send

import core.config as config
import core.research.wiring as wiring
from core.config import parse_research_lks_instances
from core.memory.fingerprint import EmbedFingerprint, FingerprintMismatch
from core.memory.lks_adapter import ReadAdapterError
from core.memory.models import Hit
from core.nodes.dispatch import _route_after_dispatch, dispatch_node
from core.research.subagent import InMemoryReportStore, RoundArtifact, run_iterresearch
from core.verify.entailment import RetrieveRequest


# ---------------------------------------------------------------------------
# Helpers / fakes (mirror tests/research/test_retrieve.py)
# ---------------------------------------------------------------------------

def hit(pid: str, score: float = 0.9, **payload) -> Hit:
    payload.setdefault("chunk_text", f"text-{pid}")
    return Hit(id=str(pid), score=score, payload=dict(payload))


class FakeLKSAdapter:
    """Canned hits per instance; records calls; can raise per instance."""

    def __init__(self, by_instance: Optional[Dict[str, List[Hit]]] = None,
                 raises: Optional[Dict[str, Exception]] = None):
        self.by_instance = by_instance or {}
        self.raises = raises or {}
        self.calls: List[tuple] = []

    async def search(self, instance: str, *, query: str, k: int = 10,
                     filters: Any = None) -> List[Hit]:
        self.calls.append((instance, query, k))
        exc = self.raises.get(instance)
        if exc is not None:
            raise exc
        return list(self.by_instance.get(instance, []))


def req(tried=()) -> RetrieveRequest:
    return RetrieveRequest(bid_key="bk", tried_sources=tuple(tried),
                           constraint=None, reason_code=None, attempt=0)


def _mismatch() -> FingerprintMismatch:
    return FingerprintMismatch(
        EmbedFingerprint("m-a", 768, True, "cosine"),
        EmbedFingerprint("m-b", 768, True, "cosine"),
        ["model_id"],
        "ctx",
    )


@pytest.fixture(autouse=True)
def _clean_wiring_cache():
    """Every test starts and ends with a cold adapter cache (module-global state)."""
    wiring.reset_adapter_cache()
    yield
    wiring.reset_adapter_cache()


def _enable(monkeypatch, instances=("hermes",), adapter=None, ok=None):
    """Turn the lane ON: set the config tuple and stub the built adapter."""
    monkeypatch.setattr(config, "RESEARCH_LKS_INSTANCES", tuple(instances))
    if adapter is not None:
        monkeypatch.setattr(
            wiring, "_build_adapter",
            lambda _inst: (adapter, tuple(ok if ok is not None else instances)),
        )


# ---------------------------------------------------------------------------
# 1. Env parse — strict, order-preserving, dedup, whitespace-tolerant
# ---------------------------------------------------------------------------

class TestParseResearchLksInstances:
    def test_empty(self):
        assert parse_research_lks_instances("") == ()

    def test_one(self):
        assert parse_research_lks_instances("hermes") == ("hermes",)

    def test_many_order_preserved(self):
        assert parse_research_lks_instances("hermes,wiki,sps") == ("hermes", "wiki", "sps")

    def test_whitespace_stripped(self):
        assert parse_research_lks_instances("  hermes ,\twiki  ") == ("hermes", "wiki")

    def test_empty_segments_dropped(self):
        assert parse_research_lks_instances("hermes,,wiki,") == ("hermes", "wiki")
        assert parse_research_lks_instances(" , ,") == ()

    def test_duplicates_collapse_to_first(self):
        assert parse_research_lks_instances("hermes,wiki,hermes") == ("hermes", "wiki")

    def test_none_like_default(self):
        assert parse_research_lks_instances(None or "") == ()


# ---------------------------------------------------------------------------
# 2. Spec gating — empty env is a no-op; set env builds a bound retriever
# ---------------------------------------------------------------------------

class TestSpecGating:
    def test_empty_env_no_spec(self, monkeypatch):
        monkeypatch.setattr(config, "RESEARCH_LKS_INSTANCES", ())
        assert wiring.research_subagent_spec("what is the sentinel?") is None

    def test_empty_env_attach_is_byte_identical(self, monkeypatch):
        monkeypatch.setattr(config, "RESEARCH_LKS_INSTANCES", ())
        args = [{"task": "q1"}, {"task": "q2"}]
        before = [dict(a) for a in args]
        assert wiring.attach_research_specs(args) == 0
        assert args == before  # no keys added, nothing mutated

    def test_availability_degrade_no_spec(self, monkeypatch):
        """Adapter build failure (memory down, registry missing) ⇒ fail OPEN: None, no raise."""
        _enable(monkeypatch)
        monkeypatch.setattr(wiring, "_build_adapter", lambda _inst: (None, ()))
        assert wiring.research_subagent_spec("q") is None

    def test_spec_carries_question_retriever_store(self, monkeypatch):
        adapter = FakeLKSAdapter({"hermes": [hit("h1")]})
        _enable(monkeypatch, adapter=adapter)
        spec = wiring.research_subagent_spec("  what is the sentinel?  ")
        assert spec is not None
        assert spec["question"] == "what is the sentinel?"
        assert callable(spec["retriever"])
        assert isinstance(spec["report_store"], InMemoryReportStore)

    def test_each_spec_gets_its_own_report_store(self, monkeypatch):
        _enable(monkeypatch, adapter=FakeLKSAdapter())
        s1 = wiring.research_subagent_spec("q1")
        s2 = wiring.research_subagent_spec("q2")
        assert s1["report_store"] is not s2["report_store"]

    def test_blank_question_no_spec(self, monkeypatch):
        _enable(monkeypatch, adapter=FakeLKSAdapter())
        assert wiring.research_subagent_spec("   ") is None

    def test_attach_decorates_each_send_arg(self, monkeypatch):
        _enable(monkeypatch, adapter=FakeLKSAdapter())
        args = [{"task": "q1"}, {"task": ""}, {"task": "q3"}]
        assert wiring.attach_research_specs(args) == 2
        assert "research_subagent" in args[0] and "research_subagent" in args[2]
        assert "research_subagent" not in args[1]  # blank task stays a plain worker

    def test_attached_specs_are_serializable_markers(self, monkeypatch):
        """Send args ride through the graph checkpointer — the live-smoke msgpack defect
        (2026-07-20): NOTHING non-serializable may be attached at the fan-out edge."""
        import json

        _enable(monkeypatch, adapter=FakeLKSAdapter())
        args = [{"task": "q1"}, {"task": "q2"}]
        wiring.attach_research_specs(args)
        for arg in args:
            assert arg["research_subagent"] == {"question": arg["task"]}
            json.dumps(arg["research_subagent"])  # plain data, checkpointer-safe

    def test_retriever_reads_the_configured_instance(self, monkeypatch):
        adapter = FakeLKSAdapter({"hermes": [hit("h1", chunk_text="evidence body")]})
        _enable(monkeypatch, adapter=adapter)
        spec = wiring.research_subagent_spec("what is the sentinel?")

        import asyncio
        source = asyncio.run(spec["retriever"](req()))
        assert source is not None
        assert source.id.startswith("lks:hermes:")
        assert source.text == "evidence body"
        # the retriever queried the bound question against the hermes instance
        assert adapter.calls and adapter.calls[0][0] == "hermes"
        assert adapter.calls[0][1] == "what is the sentinel?"


# ---------------------------------------------------------------------------
# 3. Adapter cache — success cached, failure retried, reset works
# ---------------------------------------------------------------------------

class TestAdapterCache:
    def test_memory_not_bootstrapped_fails_open_and_is_not_cached(self, monkeypatch):
        import core.memory.bootstrap as bootstrap
        from core.memory.exceptions import MemoryConfigError

        def not_booted():
            raise MemoryConfigError("memory not bootstrapped")

        monkeypatch.setattr(bootstrap, "get_memory", not_booted)
        assert wiring._build_adapter(("hermes",)) == (None, ())
        # a failed build must NOT poison the cache — a later turn retries
        assert wiring._adapter_cache is None

    def test_successful_build_is_cached(self, monkeypatch):
        import core.memory.bootstrap as bootstrap

        class _Prov:
            _client = object()

        class _Mem:
            slot_resolver = object()
            retriever = type("R", (), {"_provider": _Prov()})()

        calls = []

        def fake_builder(slot_resolver, client, instances, *, seam):
            calls.append(instances)
            return ("ADAPTER", tuple(instances))

        monkeypatch.setattr(bootstrap, "get_memory", lambda: _Mem())
        monkeypatch.setattr(bootstrap, "build_lks_read_adapter", fake_builder)

        assert wiring._build_adapter(("hermes",)) == ("ADAPTER", ("hermes",))
        assert wiring._build_adapter(("hermes",)) == ("ADAPTER", ("hermes",))
        assert len(calls) == 1  # second call served from cache

        wiring.reset_adapter_cache()
        wiring._build_adapter(("hermes",))
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# 4. Failure split at read time — availability OPEN, fingerprint/ACL CLOSED
# ---------------------------------------------------------------------------

class TestReadFailureSplit:
    @pytest.mark.asyncio
    async def test_fingerprint_mismatch_fails_closed(self, monkeypatch):
        """The brief's hard constraint: a 4-field embed mismatch must keep failing the read."""
        adapter = FakeLKSAdapter(raises={"hermes": _mismatch()})
        _enable(monkeypatch, adapter=adapter)
        spec = wiring.research_subagent_spec("q")
        with pytest.raises(FingerprintMismatch):
            await spec["retriever"](req())

    @pytest.mark.asyncio
    async def test_availability_error_fails_open_to_a_miss(self, monkeypatch):
        """Qdrant/embedder hiccup (ReadAdapterError) ⇒ instance miss, not a crash (no web tool ⇒ None)."""
        adapter = FakeLKSAdapter(raises={"hermes": ReadAdapterError("conn refused")})
        _enable(monkeypatch, adapter=adapter)
        spec = wiring.research_subagent_spec("q")
        assert await spec["retriever"](req()) is None


# ---------------------------------------------------------------------------
# 5. Shared builder pre-flight (bootstrap.build_lks_read_adapter)
# ---------------------------------------------------------------------------

STAMP = {"model_id": "qwen3-embedding-4b", "dim": 2560, "normalize": True, "distance": "cosine"}
ACL = {"writer": "hermes", "readers": ["bobclaw", "hermes"], "mode": "ro"}


class TestSharedBuilder:
    @pytest.fixture
    def registry_file(self, tmp_path, monkeypatch):
        """A throwaway on-disk registry: one stamped instance, one unstamped. Never the real file."""
        from core.ledger.federation import FederationRegistry

        path = tmp_path / "reg.json"
        reg = FederationRegistry(path)
        reg.register("hermes", "/tmp/hermes-corpus", collection="hermes__2560", dim=2560,
                     meta={"embed": dict(STAMP), "acl": dict(ACL)})
        reg.register("unstamped", "/tmp/other", collection="other__2560", dim=2560,
                     meta={"acl": dict(ACL)})
        reg.save()
        monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(path))
        monkeypatch.delenv("MEMORY_LKS_QDRANT_URL", raising=False)
        return path

    @pytest.fixture
    def slot_resolver(self):
        from pathlib import Path

        from core.memory.slots import SlotResolver

        cfg = Path(__file__).resolve().parents[2] / "config" / "memory_slots.toml"
        return SlotResolver(cfg)

    def test_stamped_survives_unknown_and_unstamped_drop(self, registry_file, slot_resolver):
        from core.memory.bootstrap import build_lks_read_adapter

        adapter, ok = build_lks_read_adapter(
            slot_resolver, object(), ("hermes", "ghost", "unstamped"), seam="test"
        )
        assert adapter is not None
        assert ok == ("hermes",)

    def test_nothing_survives_degrades_to_none(self, registry_file, slot_resolver):
        from core.memory.bootstrap import build_lks_read_adapter

        adapter, ok = build_lks_read_adapter(
            slot_resolver, object(), ("ghost", "unstamped"), seam="test"
        )
        assert adapter is None and ok == ()

    def test_empty_instances_is_inert(self, registry_file, slot_resolver):
        from core.memory.bootstrap import build_lks_read_adapter

        assert build_lks_read_adapter(slot_resolver, object(), (), seam="test") == (None, ())

    def test_missing_registry_file_degrades(self, tmp_path, monkeypatch, slot_resolver):
        from core.memory.bootstrap import build_lks_read_adapter

        monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(tmp_path / "absent.json"))
        monkeypatch.delenv("MEMORY_LKS_QDRANT_URL", raising=False)
        adapter, ok = build_lks_read_adapter(
            slot_resolver, object(), ("hermes",), seam="test"
        )
        assert adapter is None and ok == ()

    def test_recall_seam_still_wired_through_shared_builder(self, registry_file, slot_resolver, monkeypatch):
        """_maybe_build_lks_adapter (MEMORY_LKS_* gating) rides the same builder unchanged."""
        from core.memory.bootstrap import _maybe_build_lks_adapter

        monkeypatch.setenv("MEMORY_LKS_FIRST", "true")
        monkeypatch.setenv("MEMORY_LKS_INSTANCE", "hermes")
        adapter, instance, enabled = _maybe_build_lks_adapter(slot_resolver, object())
        assert enabled is True and instance == "hermes" and adapter is not None

        monkeypatch.setenv("MEMORY_LKS_INSTANCE", "unstamped")
        assert _maybe_build_lks_adapter(slot_resolver, object()) == (None, None, False)

        monkeypatch.setenv("MEMORY_LKS_FIRST", "false")
        assert _maybe_build_lks_adapter(slot_resolver, object()) == (None, None, False)


# ---------------------------------------------------------------------------
# 6. The dispatch edge gate — the ONE live-graph touch point
# ---------------------------------------------------------------------------

def _research_state(**overrides) -> dict:
    base = {
        "task": "research the sentinel",
        "face_id": "assistant",
        "backend": "deepseek_v4",
        "messages": [],
        "subtasks": ["q-one", "q-two", "q-three"],
        "fanout_width": 3,
        "research_tier": "fanout",
        "escalation_backend": None,
    }
    base.update(overrides)
    return base


def _route(**overrides):
    st = _research_state(**overrides)
    st.update(dispatch_node(st))
    return _route_after_dispatch(st)


class TestDispatchEdgeGate:
    def test_research_turn_with_env_attaches_specs(self, monkeypatch):
        _enable(monkeypatch, adapter=FakeLKSAdapter())
        sends = _route()
        assert isinstance(sends, list) and all(isinstance(s, Send) for s in sends)
        assert all(s.arg.get("research_subagent") is not None for s in sends)
        qs = [s.arg["research_subagent"]["question"] for s in sends]
        assert qs == ["q-one", "q-two", "q-three"]

    def test_research_turn_with_empty_env_is_plain(self, monkeypatch):
        monkeypatch.setattr(config, "RESEARCH_LKS_INSTANCES", ())
        sends = _route()
        assert isinstance(sends, list)
        assert all("research_subagent" not in s.arg for s in sends)

    def test_plain_chat_fanout_never_gets_specs_even_with_env(self, monkeypatch):
        _enable(monkeypatch, adapter=FakeLKSAdapter())
        sends = _route(research_tier=None)
        assert isinstance(sends, list)
        assert all("research_subagent" not in s.arg for s in sends)


# ---------------------------------------------------------------------------
# 7. Worker-side runtime prep — the marker spec becomes a runnable spec (or falls open)
# ---------------------------------------------------------------------------

class TestPrepareResearchSpec:
    def test_marker_spec_is_filled_at_runtime(self, monkeypatch):
        from core.nodes.worker import _prepare_research_spec

        _enable(monkeypatch, adapter=FakeLKSAdapter({"hermes": [hit("h1")]}))
        sub_state = {"research_subagent": {"question": "q1"}, "task": "q1"}
        assert _prepare_research_spec(sub_state) is True
        spec = sub_state["research_subagent"]
        assert callable(spec["retriever"])
        assert isinstance(spec["report_store"], InMemoryReportStore)
        assert spec["question"] == "q1"

    def test_lane_off_falls_open(self, monkeypatch):
        from core.nodes.worker import _prepare_research_spec

        monkeypatch.setattr(config, "RESEARCH_LKS_INSTANCES", ())
        sub_state = {"research_subagent": {"question": "q1"}}
        assert _prepare_research_spec(sub_state) is False
        assert sub_state["research_subagent"] == {"question": "q1"}  # untouched marker

    def test_availability_degrade_falls_open(self, monkeypatch):
        from core.nodes.worker import _prepare_research_spec

        _enable(monkeypatch)
        monkeypatch.setattr(wiring, "_build_adapter", lambda _inst: (None, ()))
        assert _prepare_research_spec({"research_subagent": {"question": "q1"}}) is False

    def test_full_spec_is_used_as_is(self, monkeypatch):
        from core.nodes.worker import _prepare_research_spec

        monkeypatch.setattr(config, "RESEARCH_LKS_INSTANCES", ())  # even with the lane OFF
        retriever, store = object(), object()
        sub_state = {"research_subagent": {
            "question": "q1", "retriever": retriever, "report_store": store,
        }}
        assert _prepare_research_spec(sub_state) is True
        assert sub_state["research_subagent"]["retriever"] is retriever
        assert sub_state["research_subagent"]["report_store"] is store


# ---------------------------------------------------------------------------
# 8. End-to-end: hermes-only evidence reaches the condensed return with provenance
# ---------------------------------------------------------------------------

class TestHermesProvenanceEndToEnd:
    @pytest.mark.asyncio
    async def test_iterresearch_surfaces_hermes_source(self, monkeypatch):
        """Evidence that lives ONLY in the hermes instance lands in the worker's condensed
        return (the join-visible content) with ``lks:hermes:*`` provenance."""
        sentinel = "The corpus sentinel is BOB-HERMES-7."
        adapter = FakeLKSAdapter({"hermes": [hit("chunk-42", chunk_text=sentinel)]})
        _enable(monkeypatch, adapter=adapter)
        spec = wiring.research_subagent_spec("what is the corpus sentinel?")

        async def model_send(messages, backend):
            # the retrieved chunk must be IN the model's workspace (it shapes the prompt)
            joined = "\n".join(m.get("content", "") for m in messages)
            assert sentinel in joined
            return "The sentinel is BOB-HERMES-7."

        def parser(reply, round_idx, source):
            return RoundArtifact(round_idx=round_idx, claims=(), sources=(),
                                 report_fragment=reply)

        cr, _traces = await run_iterresearch(
            question=spec["question"],
            retriever=spec["retriever"],
            model_send=model_send,
            backend="fake",
            report_store=spec["report_store"],
            round_parser=parser,
            max_rounds=1,
        )

        content = cr.to_content()
        assert "lks:hermes:chunk-42" in content  # provenance crosses the firewall to the join
        assert any(s["id"].startswith("lks:hermes:") for s in cr.sources)
