"""MS2-R3 — unit tests for the research subagent (IterResearch loop + condensed-return firewall).

PURE: no network, no real git/Qdrant/model. All collaborators (report store, retriever, model_send) are
injected; the worker_node cases monkeypatch ``core.nodes.worker._send_to_backend``. asyncio_mode=auto.
"""
import json

import pytest

from core.research.subagent import (
    ResearchSubagentError,
    RoundArtifact,
    RoundTrace,
    CondensedReturn,
    build_lean_workspace,
    condense_tool_turn,
    enforce_return_ceiling,
    run_iterresearch,
    LedgerReportStore,
    _default_round_parser,
)
from core.config import (
    RESEARCH_RETURN_TOKEN_CEILING,
    RESEARCH_MAX_ROUNDS,
    RESEARCH_MAX_CLAIMS,
    RESEARCH_MAX_SOURCES,
)
from core.nodes.budget_runtime import approx_tokens
from core.ledger.types import OVERSPEND_TRIGGER
from core.verify.entailment import Source, SourceKind, RetrieveRequest
from core.nodes.worker import worker_node


# ── Helper fakes ──────────────────────────────────────────────────────────────

class FakeReportStore:
    """In-memory emulation of the durable ledger slice: read_report joins committed fragments."""

    def __init__(self):
        self.fragments = []
        self.appended = []
        self.read_calls = 0

    async def read_report(self):
        self.read_calls += 1
        return "\n\n".join(a.report_fragment for a in self.fragments if a.report_fragment)

    async def append_fragment(self, artifact):
        self.fragments.append(artifact)
        self.appended.append(artifact)


def src(i, text=None, kind=SourceKind.VENDOR):
    return Source(id=f"s{i}", text=(text if text is not None else f"raw-chunk-{i} " * 50), kind=kind)


class FakeRetriever:
    """Returns canned sources, honors tried_sources (returns the first untried), then None."""

    def __init__(self, sources):
        self.sources = list(sources)
        self.reqs = []

    async def retrieve(self, req):
        self.reqs.append(req)
        tried = set(req.tried_sources or ())
        for s in self.sources:
            if s.id not in tried:
                return s
        return None


def round_json(i, fragment=None, claims=None):
    return json.dumps({
        "claims": claims if claims is not None else [{"subject": "x", "predicate": "is", "numeric_value": i}],
        "report_fragment": fragment if fragment is not None else f"frag-{i}",
    })


def counting_send(reply_for):
    """An async model_send that returns reply_for(round_idx); tracks the round via a counter."""
    state = {"n": 0}

    async def _send(messages, backend):
        i = state["n"]
        state["n"] += 1
        return reply_for(i, messages)

    _send.state = state
    return _send


# ── 1. ≥2 rounds reconstruct from the ledger slice ───────────────────────────

async def test_two_rounds_reconstruct_from_ledger_slice():
    store = FakeReportStore()
    retr = FakeRetriever([src(0), src(1), src(2)])
    send = counting_send(lambda i, m: round_json(i))

    cr, traces = await run_iterresearch(
        question="q", retriever=retr.retrieve, model_send=send, backend="b",
        report_store=store, max_rounds=2, branch_budget=None,
    )

    assert len(traces) == 2
    assert all(t.reconstructed_from_ledger for t in traces)
    # the store was actually READ once per round (the reconstruction is from the ledger, not trivially-green)
    assert store.read_calls == 2
    assert traces[0].evolving_report_tokens == 0    # round 0 reads ""
    assert traces[1].evolving_report_tokens > 0      # round 1 reads round 0's committed fragment
    assert len(store.appended) == 2                  # one durable commit per round-artifact


# ── 2. ephemera dropped — by token-count AND by content ──────────────────────

async def test_ephemera_dropped_by_token_and_content():
    store = FakeReportStore()
    # markers placed PAST the condense_tool_turn 200-char snippet, so the full raw chunk is genuinely dropped
    # (only a bounded condensed reference + the durable condensed claim are carried into the next round).
    sources = [
        src(0, text=("filler " * 400) + " EPHEMERAL_MARK_0"),
        src(1, text=("filler " * 400) + " EPHEMERAL_MARK_1"),
    ]
    retr = FakeRetriever(sources)
    captured = []

    async def send(messages, backend):
        captured.append(messages)
        i = len(captured) - 1
        return round_json(i, fragment=f"CONDENSED_CLAIM_{i}", claims=[])

    cr, traces = await run_iterresearch(
        question="q", retriever=retr.retrieve, model_send=send, backend="b",
        report_store=store, max_rounds=2, branch_budget=None,
    )

    # (a) round-1's reconstructed WORKSPACE (system+user, excluding the fresh tool turn) carries the round-0
    # CONDENSED claim via the evolving report but NOT the round-0 raw ephemeral marker.
    round1_workspace_text = " ".join(m["content"] for m in captured[1][:2])
    assert "EPHEMERAL_MARK_0" not in round1_workspace_text       # the round-0 raw chunk was DROPPED
    assert "CONDENSED_CLAIM_0" in round1_workspace_text          # only its condensed claim carried forward
    # the round-1 fresh tool turn carries round-1's raw chunk (that is the CURRENT round's tool turn, allowed)
    assert "EPHEMERAL_MARK_1" in captured[1][2]["content"]
    # the round-1 fresh tool turn does NOT re-introduce the round-0 raw chunk
    assert "EPHEMERAL_MARK_0" not in captured[1][2]["content"]

    # (b) ephemera were actually discarded each round.
    assert all(t.dropped_ephemera_tokens > 0 for t in traces)

    # (c) by token-count: the workspace stays bounded — far below the cumulative raw-chunk token sum.
    cumulative_raw = approx_tokens(sources[0].text) + approx_tokens(sources[1].text)
    assert traces[1].workspace_tokens < cumulative_raw / 2


# ── 3. condensed return ≤ ceiling (the firewall) — structured ────────────────

async def test_condensed_return_ceiling_enforced():
    store = FakeReportStore()
    retr = FakeRetriever([src(0), src(1)])

    async def send(messages, backend):
        return round_json(0, fragment="BIG " * 5000)

    cr, _ = await run_iterresearch(
        question="q", retriever=retr.retrieve, model_send=send, backend="b",
        report_store=store, max_rounds=2, branch_budget=None,
        return_ceiling=RESEARCH_RETURN_TOKEN_CEILING,
    )

    assert cr.token_count() <= RESEARCH_RETURN_TOKEN_CEILING
    assert cr.return_tokens <= RESEARCH_RETURN_TOKEN_CEILING
    assert cr.truncated is True
    content = json.loads(cr.to_content())
    assert isinstance(content, dict)
    assert set(content.keys()) == {"claims", "sources", "report_fragment"}
    # the structured claims/sources SURVIVE while the free-text fragment is the truncated part
    assert len(content["claims"]) >= 1
    assert len(content["sources"]) >= 1


async def test_condensed_return_structured_alone_too_big():
    """A tiny ceiling forces the structured-alone branch (fragment -> "", lists clamped)."""
    cl = tuple({"subject": str(i), "predicate": "p", "numeric_value": i, "cited_source_id": f"s{i}"} for i in range(8))
    so = tuple({"id": f"s{i}", "kind": "vendor"} for i in range(8))
    claims, sources, frag, truncated = enforce_return_ceiling(
        cl, so, "BIG " * 200, ceiling=20, max_claims=8, max_sources=8,
    )
    assert frag == ""
    assert truncated is True
    content = json.dumps(
        {"claims": list(claims), "sources": list(sources), "report_fragment": frag},
        ensure_ascii=False, separators=(",", ":"),
    )
    assert approx_tokens(content) <= 20


# ── 4. firewall: internal burn ≫ return ──────────────────────────────────────

async def test_firewall_internal_burn_exceeds_return():
    store = FakeReportStore()
    retr = FakeRetriever([src(0, text="RAWBURN " * 2000), src(1, text="RAWBURN " * 2000)])

    async def send(messages, backend):
        return round_json(0, fragment="BIG " * 5000)

    cr, _ = await run_iterresearch(
        question="q", retriever=retr.retrieve, model_send=send, backend="b",
        report_store=store, max_rounds=2, branch_budget=None,
    )

    assert cr.internal_burn_tokens > cr.return_tokens * 3       # burned internally, returned ≤ ceiling
    assert cr.token_count() <= RESEARCH_RETURN_TOKEN_CEILING    # the return is bounded by the firewall ceiling
    assert "RAWBURN" not in cr.to_content()                    # the raw tool output NEVER surfaces (the firewall)


# ── 5. planted runaway round trips the in-branch breaker (BIND-02) ───────────

async def test_runaway_round_trips_breaker():
    async def huge(messages, backend):
        return "X " * 8000

    cr_tiny, _ = await run_iterresearch(
        question="q", retriever=FakeRetriever([src(0)] * 4).retrieve, model_send=huge, backend="b",
        report_store=FakeReportStore(), max_rounds=4,
        branch_budget={"reservation": 50, "trigger": OVERSPEND_TRIGGER},
    )
    assert cr_tiny.breaker_tripped is True
    assert cr_tiny.rounds < 4
    assert cr_tiny.budget is not None and cr_tiny.budget["tripped"] is True

    # the SAME run with a generous reservation does NOT trip (the breaker is the discriminator).
    cr_big, _ = await run_iterresearch(
        question="q", retriever=FakeRetriever([src(0)] * 4).retrieve, model_send=huge, backend="b",
        report_store=FakeReportStore(), max_rounds=4,
        branch_budget={"reservation": 10_000_000, "trigger": OVERSPEND_TRIGGER},
    )
    assert cr_big.breaker_tripped is False
    assert cr_big.rounds == 4


# ── 6. no budget ⇒ no breaker, byte-identical loop ───────────────────────────

async def test_no_budget_no_breaker():
    async def send(messages, backend):
        return round_json(0, fragment="small", claims=[])

    cr, _ = await run_iterresearch(
        question="q", retriever=FakeRetriever([src(0)] * 2).retrieve, model_send=send, backend="b",
        report_store=FakeReportStore(), max_rounds=2, branch_budget=None,
    )
    assert cr.budget is None
    assert cr.breaker_tripped is False
    assert cr.rounds == 2


# ── 7. build_lean_workspace carries ONLY {question + evolving_report + last_tool_turn} ──

def test_build_lean_workspace_carries_only_three():
    ws = build_lean_workspace("the question", "the evolving report", "the last tool turn", instructions="INSTR")
    joined = " ".join(m["content"] for m in ws)
    assert "the question" in joined
    assert "the evolving report" in joined
    assert "the last tool turn" in joined
    assert "INSTR" in joined
    assert "PRIOR_ROUND_RAW_EPHEMERA" not in joined
    assert len(ws) == 2
    assert ws[0]["role"] == "system" and ws[1]["role"] == "user"
    # a default instruction is supplied when none is given
    ws2 = build_lean_workspace("q", "r", "t")
    assert ws2[0]["content"].strip()


# ── 8. enforce_return_ceiling unit ───────────────────────────────────────────

def _content_tokens(claims, sources, fragment):
    return approx_tokens(json.dumps(
        {"claims": list(claims), "sources": list(sources), "report_fragment": fragment},
        ensure_ascii=False, separators=(",", ":"),
    ))


def test_enforce_return_ceiling_unit():
    claims = ({"subject": "a", "predicate": "b", "numeric_value": 1},)
    sources = ({"id": "s0", "kind": "vendor"},)

    # (a) under-ceiling input passes unchanged
    c, s, f, t = enforce_return_ceiling(claims, sources, "short", ceiling=1000, max_claims=8, max_sources=8)
    assert t is False
    assert f == "short"
    assert _content_tokens(c, s, f) <= 1000

    # (b) over-ceiling truncates the fragment
    c, s, f, t = enforce_return_ceiling(claims, sources, "a" * 4000, ceiling=40, max_claims=8, max_sources=8)
    assert t is True
    assert _content_tokens(c, s, f) <= 40

    # (c) > max_claims / > max_sources capped
    many_c = tuple({"subject": str(i), "predicate": "p", "numeric_value": i} for i in range(15))
    many_s = tuple({"id": f"s{i}", "kind": "vendor"} for i in range(12))
    c, s, f, t = enforce_return_ceiling(many_c, many_s, "text", ceiling=100000, max_claims=8, max_sources=8)
    assert len(c) == 8 and len(s) == 8 and t is True

    # (d) structured-alone over a modest ceiling ⇒ fragment "" and lists clamped, still ≤ ceiling
    c, s, f, t = enforce_return_ceiling(many_c, many_s, "BIG TEXT", ceiling=20, max_claims=8, max_sources=8)
    assert f == ""
    assert t is True
    assert _content_tokens(c, s, f) <= 20


# ── 9. _default_round_parser tolerant ────────────────────────────────────────

def test_default_round_parser_tolerant():
    source = src(7)

    # (a) plain JSON
    a = _default_round_parser('{"claims":[{"subject":"a","predicate":"b","numeric_value":1}],"report_fragment":"rf"}', 0, source)
    assert len(a.claims) == 1 and a.report_fragment == "rf"
    assert a.claims[0]["cited_source_id"] == "s7"       # defaulted to the round's source

    # (b) fenced JSON block
    fenced = "```json\n" + '{"claims":[],"report_fragment":"fenced"}' + "\n```"
    b = _default_round_parser(fenced, 1, source)
    assert b.report_fragment == "fenced"

    # (c) JSON embedded in prose — the LAST balanced object wins
    prose = 'thinking... {"verdict":"x"} then the answer {"claims":[],"report_fragment":"final"}'
    c = _default_round_parser(prose, 2, source)
    assert c.report_fragment == "final"

    # (d) no JSON — whole reply becomes report_fragment, zero claims
    d = _default_round_parser("just some prose", 3, source)
    assert d.report_fragment == "just some prose" and d.claims == ()

    # (e) None source ⇒ no cited_source_id default, sources empty
    e = _default_round_parser('{"claims":[{"subject":"a","predicate":"b"}],"report_fragment":"x"}', 4, None)
    assert e.sources == ()
    assert "cited_source_id" not in e.claims[0] or not e.claims[0].get("cited_source_id")


# ── 10. tried_sources decorrelation across rounds ────────────────────────────

async def test_tried_sources_decorrelation_across_rounds():
    retr = FakeRetriever([src(0), src(1), src(2)])
    send = counting_send(lambda i, m: round_json(i))
    cr, traces = await run_iterresearch(
        question="q", retriever=retr.retrieve, model_send=send, backend="b",
        report_store=FakeReportStore(), max_rounds=3, branch_budget=None,
    )
    assert [t.source_id for t in traces] == ["s0", "s1", "s2"]
    assert {s["id"] for s in cr.sources} == {"s0", "s1", "s2"}


# ── 11. retriever exhausted mid-run (None) is graceful ───────────────────────

async def test_retriever_exhausted_midrun_graceful():
    retr = FakeRetriever([src(0)])         # only one source
    send = counting_send(lambda i, m: round_json(i))
    cr, traces = await run_iterresearch(
        question="q", retriever=retr.retrieve, model_send=send, backend="b",
        report_store=FakeReportStore(), max_rounds=3, branch_budget=None,
    )
    assert traces[0].source_id == "s0"
    assert traces[1].source_id is None and traces[2].source_id is None
    assert cr.rounds == 3                  # the loop still ran (synthesizing from the evolving report)


# ── 12. worker_node research branch — guard + firewall ───────────────────────

async def test_worker_node_research_branch_firewall(monkeypatch):
    send = counting_send(lambda i, m: round_json(i))
    monkeypatch.setattr("core.nodes.worker._send_to_backend", send)
    spec = {"question": "q", "retriever": FakeRetriever([src(0), src(1)]).retrieve,
            "report_store": FakeReportStore(), "max_rounds": 2}
    out = await worker_node({"research_subagent": spec, "subtask_idx": 2, "backend": "x"})
    e = out["worker_results"][0]
    assert e["status"] == "ok"
    assert e["idx"] == 2
    assert approx_tokens(e["content"]) <= RESEARCH_RETURN_TOKEN_CEILING
    assert e["internal_burn_tokens"] > e["return_tokens"]
    assert e["rounds"] == 2


# ── 13. worker_node research breaker → entry["budget"] ───────────────────────

async def test_worker_node_research_breaker_to_entry_budget(monkeypatch):
    async def huge(messages, backend):
        return "X " * 8000
    monkeypatch.setattr("core.nodes.worker._send_to_backend", huge)
    spec = {"question": "q", "retriever": FakeRetriever([src(0)] * 4).retrieve,
            "report_store": FakeReportStore(), "max_rounds": 4}
    out = await worker_node({
        "research_subagent": spec, "subtask_idx": 0, "backend": "x",
        "branch_budget": {"reservation": 30, "trigger": OVERSPEND_TRIGGER},
    })
    e = out["worker_results"][0]
    assert e["budget"]["tripped"] is True
    assert e["breaker_tripped"] is True
    # _meter_branch read the REAL internal burn (entry["usage"].total_tokens), NOT the question+content text:
    # the budget's spent must equal the subagent's internal_burn_tokens (the discriminator).
    assert e["budget"]["spent"] == e["internal_burn_tokens"]


# ── 14. worker_node BYTE-IDENTICAL for non-research ──────────────────────────

async def test_worker_node_byte_identical_non_research(monkeypatch):
    async def chat(messages, backend):
        return "chat-reply"
    monkeypatch.setattr("core.nodes.worker._send_to_backend", chat)
    out = await worker_node({"task": "hello", "backend": "x", "subtask_idx": 0, "messages": []})
    e = out["worker_results"][0]
    assert e["status"] == "ok"
    assert e["content"] == "chat-reply"
    assert e["text"] == "hello"
    # the research keys are ABSENT on a chat entry — the guard added no observable change
    assert "internal_burn_tokens" not in e
    assert "breaker_tripped" not in e


async def test_worker_node_build_precedence_preserved(monkeypatch):
    """A build Send wins even when research_subagent is ALSO present (build precedence over research)."""
    async def build_send(messages, backend):
        return "def f():\n    return 1\n"
    monkeypatch.setattr("core.nodes.worker._send_to_backend", build_send)
    contract = {"name": "f", "signature": "def f():", "doc": "", "cases": []}
    research_spec = {"question": "q", "retriever": FakeRetriever([src(0)]).retrieve,
                     "report_store": FakeReportStore(), "max_rounds": 2}
    # BOTH keys present ⇒ the build branch (checked first) wins; the research branch is NOT taken.
    out = await worker_node({"build_contract": contract, "research_subagent": research_spec,
                             "backend": "x", "subtask_idx": 0})
    assert "build_impls" in out and "worker_results" not in out   # build precedence: the build reducer field
    assert out["build_impls"][0]["name"] == "f"


# ── 15. worker_node research timeout/exception ⇒ retryable entry (cattle-retry) ──

async def test_worker_node_research_failure_is_retryable_entry(monkeypatch):
    async def boom(messages, backend):
        raise RuntimeError("boom")
    monkeypatch.setattr("core.nodes.worker._send_to_backend", boom)
    spec = {"question": "q", "retriever": FakeRetriever([src(0)]).retrieve,
            "report_store": FakeReportStore(), "max_rounds": 2}
    out = await worker_node({"research_subagent": spec, "subtask_idx": 0, "backend": "x"})
    e = out["worker_results"][0]
    assert e["status"] in ("failed", "rate_limit")       # never an unhandled raise out of worker_node


# ── 15b. worker_node research TIMEOUT ⇒ status="timeout" (the explicit cattle-retry arm) ──

async def test_worker_node_research_timeout_is_retryable_entry(monkeypatch):
    import asyncio as _asyncio

    async def _raise_timeout(**kwargs):
        raise _asyncio.TimeoutError()

    monkeypatch.setattr("core.nodes.worker.run_iterresearch", _raise_timeout)
    spec = {"question": "q", "retriever": FakeRetriever([src(0)]).retrieve,
            "report_store": FakeReportStore(), "max_rounds": 2}
    out = await worker_node({"research_subagent": spec, "subtask_idx": 0, "backend": "x"})
    e = out["worker_results"][0]
    assert e["status"] == "timeout"
    assert "exceeded" in e["error"]
    assert "duration_ms" in e


# ── 16. internal burn is NOT double-counted (the raw tool output lives in the messages once) ──

async def test_internal_burn_not_double_counted():
    captured = []

    async def send(messages, backend):
        captured.append([dict(m) for m in messages])
        return round_json(0, fragment="f", claims=[])

    store = FakeReportStore()
    cr, _ = await run_iterresearch(
        question="q", retriever=FakeRetriever([src(0, text="RAW " * 200), src(1, text="RAW " * 200)]).retrieve,
        model_send=send, backend="b", report_store=store, max_rounds=2, branch_budget=None,
    )
    # the true burn = the messages SENT (which already include the raw tool output once) + each reply.
    expected = 0
    for msgs in captured:
        expected += sum(approx_tokens(m.get("content", "")) for m in msgs)
        expected += approx_tokens(round_json(0, fragment="f", claims=[]))
    assert cr.internal_burn_tokens == expected      # NOT inflated by re-counting the raw chunk


# ── 17. a sub-floor ceiling degrades gracefully (no crash) ───────────────────

async def test_subfloor_ceiling_graceful():
    cr, _ = await run_iterresearch(
        question="q", retriever=FakeRetriever([src(0)]).retrieve,
        model_send=counting_send(lambda i, m: round_json(i, fragment="BIG " * 500)),
        backend="b", report_store=FakeReportStore(), max_rounds=1, branch_budget=None,
        return_ceiling=1,   # below the empty-skeleton floor — must NOT crash; returns the minimal skeleton
    )
    parsed = json.loads(cr.to_content())
    assert set(parsed.keys()) == {"claims", "sources", "report_fragment"}
    assert cr.truncated is True


# ── construction validation ──────────────────────────────────────────────────

async def test_construction_validation():
    store, retr, send = FakeReportStore(), FakeRetriever([src(0)]).retrieve, counting_send(lambda i, m: round_json(i))
    for bad in (
        dict(question=""),
        dict(question="q", max_rounds=0),
        dict(question="q", return_ceiling=0),
        dict(question="q", max_claims=0),
        dict(question="q", max_sources=0),
    ):
        kwargs = dict(retriever=retr, model_send=send, backend="b", report_store=store)
        kwargs.update(bad)
        with pytest.raises(ResearchSubagentError):
            await run_iterresearch(**kwargs)
    # missing model_send
    with pytest.raises(ResearchSubagentError):
        await run_iterresearch(question="q", retriever=retr, model_send=None, backend="b", report_store=store)
