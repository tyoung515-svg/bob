"""MS2-R4 — unit tests for the CitationAgent (`core/research/citation.py`).

PURE: no network, no real model, no real git/Qdrant. The extractor model (`model_send`), the entailment critic
(`send`), and the retriever are all injected. Each test pins CONCRETE behavior and FAILS a weakened gate.
"""
from __future__ import annotations

import json

import pytest

from core.research.citation import (
    CitationAgentError,
    CitationReport,
    CitationResult,
    EXTRACTION_PROMPT_TEMPLATE,
    build_extraction_prompt,
    cite_draft,
    citation_false_pass_rate,
    claims_from_structured,
    extract_claims,
    lks_retriever_factory,
    parse_extracted_claims,
    run_citation_agent,
    run_citation_gate,
)
from core.verify.entailment import Claim, RetrieveRequest, Source, SourceKind, VerificationTag
from core.verify.postcondition import decorrelated_critic_backend, family_of, is_decorrelated
from core.ledger.types import EXHAUSTED_TAG, RetryReason
from core.ses.types import Label, LabeledItem


# ── helpers ──────────────────────────────────────────────────────────────────

def mk_source(i, text, kind=SourceKind.PRIMARY):
    return Source(id=f"s{i}", text=text, kind=kind)


class FakeRetriever:
    """Returns canned sources, honoring req.tried_sources (first untried), then None; records reqs + returns."""

    def __init__(self, sources):
        self.sources = list(sources)
        self.reqs = []
        self.returns = []

    async def retrieve(self, req):
        self.reqs.append(req)
        tried = set(req.tried_sources or ())
        for s in self.sources:
            if s.id not in tried:
                self.returns.append(s)
                return s
        self.returns.append(None)
        return None


def _split_prompt(prompt):
    """Pull (number, source_text) out of a built entailment prompt ('Number: ...' / 'Source text: ...')."""
    number = ""
    if "Number:" in prompt:
        number = prompt.split("Number:", 1)[1].split("\n", 1)[0].strip()
    source_text = ""
    if "Source text:" in prompt:
        source_text = prompt.split("Source text:", 1)[1]
    return number, source_text


def number_in_source_send(reason="auto"):
    """Entailment critic: 'entailed' iff the claimed Number appears in the Source text region of the prompt."""

    async def _send(messages, backend):
        number, source_text = _split_prompt(messages[-1]["content"])
        verdict = "entailed" if (number and number in source_text) else "not_entailed"
        return json.dumps({"verdict": verdict, "reasons": [reason], "reason_code": None})

    return _send


def always_not_entailed_send(reason):
    """Entailment critic that always returns not_entailed with a FREE-TEXT reason (for the firewall test)."""

    async def _send(messages, backend):
        return json.dumps({"verdict": "not_entailed", "reasons": [reason], "reason_code": None})

    return _send


# ── A) claim extraction ──────────────────────────────────────────────────────

def test_parse_extracted_claims_tolerant():
    reply = ('{"claims":[{"subject":"Holo3","predicate":"scores","numeric_value":"77.8",'
             '"cited_source_id":"p1"}]}')
    claims = parse_extracted_claims(reply)
    assert len(claims) == 1
    c = claims[0]
    assert (c.subject, c.predicate, c.numeric_value, c.cited_source_id) == ("Holo3", "scores", "77.8", "p1")

    fenced = '```json\n{"claims":[{"subject":"A","predicate":"is","numeric_value":"5"}]}\n```'
    assert len(parse_extracted_claims(fenced)) == 1

    in_prose = ('noise {"claims":[{"subject":"old","predicate":"x","numeric_value":"1"}]} more text '
                '{"claims":[{"subject":"new","predicate":"y","numeric_value":"2"}]} tail')
    last = parse_extracted_claims(in_prose)
    assert len(last) == 1 and last[0].subject == "new"  # the LAST balanced object wins

    assert parse_extracted_claims("just prose, no json") == []

    skip = '{"claims":[{"subject":"ok","predicate":"is","numeric_value":"1"},{"numeric_value":"2"}]}'
    kept = parse_extracted_claims(skip)
    assert len(kept) == 1 and kept[0].subject == "ok"  # the no-subject/predicate item is skipped

    # a literal '}' inside a JSON string value must NOT prematurely close the object (string-literal-aware)
    brace_in_str = '{"claims":[{"subject":"f(x)}","predicate":"is","numeric_value":"3","text":"a } brace"}]}'
    braced = parse_extracted_claims(brace_in_str)
    assert len(braced) == 1 and braced[0].subject == "f(x)}" and braced[0].text == "a } brace"


async def test_extract_claims_model_step():
    calls = []

    async def fake(messages, backend):
        calls.append((messages, backend))
        return '{"claims":[{"subject":"X","predicate":"is","numeric_value":"5","cited_source_id":"p1"}]}'

    cs = await extract_claims(draft="X is 5 per p1.", backend="b", model_send=fake)
    assert len(cs) == 1 and cs[0].subject == "X"
    assert len(calls) == 1

    empty = await extract_claims(draft="   ", backend="b", model_send=fake)
    assert empty == [] and len(calls) == 1  # NO model call on an empty draft


def test_claims_from_structured():
    src = [
        {"subject": "a", "predicate": "is", "numeric_value": 1, "cited_source_id": "s1"},
        {"predicate": "x"},  # missing subject -> skipped
        Claim(subject="b", predicate="has", numeric_value=2),
    ]
    out = claims_from_structured(src)
    assert len(out) == 2
    assert {c.subject for c in out} == {"a", "b"}


def test_build_extraction_prompt_brace_safe():
    # a draft with literal braces must not raise / inject a placeholder
    prompt = build_extraction_prompt('the value is {x} and a dict {"k": 1}')
    assert 'the value is {x}' in prompt
    assert "{draft}" not in prompt  # the placeholder was substituted
    assert "claims" in EXTRACTION_PROMPT_TEMPLATE


# ── B) per-claim gate driver ─────────────────────────────────────────────────

async def test_true_claim_pv_published():
    retr = FakeRetriever([mk_source(0, "Holo3 reaches 77.8% on OSWorld.", SourceKind.PRIMARY)])
    res = await run_citation_gate(
        claim=Claim(subject="Holo3", predicate="scores", numeric_value="77.8", cited_source_id="s0"),
        retrieve=retr.retrieve, actor_backend="glm_5_2", send=number_in_source_send(),
    )
    assert res.verified is True
    assert res.tag == VerificationTag.PV
    assert res.published is True
    assert res.committed_tag is None
    assert res.decorrelated is True
    # pin the resolved critic at the PV site too (a hard-coded decorrelated=True would NOT satisfy this)
    assert res.critic_backend and is_decorrelated("glm_5_2", res.critic_backend)


async def test_true_vendor_vs():
    retr = FakeRetriever([mk_source(0, "Vendor sheet: device draws 77.8 watts.", SourceKind.VENDOR)])
    res = await run_citation_gate(
        claim=Claim(subject="device", predicate="draws", numeric_value="77.8", cited_source_id="s0"),
        retrieve=retr.retrieve, actor_backend="glm_5_2", send=number_in_source_send(),
    )
    assert res.verified is True
    assert res.tag == VerificationTag.VS  # ENTAILED + VENDOR
    assert res.published is True


async def test_wrong_claim_exhausted_surfaced():
    retr = FakeRetriever([
        mk_source(0, "Holo3 reaches 77.8% on OSWorld.", SourceKind.PRIMARY),
        mk_source(1, "An independent re-run measured 77.8%.", SourceKind.PRIMARY),
        mk_source(2, "The leaderboard lists 77.8.", SourceKind.PRIMARY),
    ])
    claim = Claim(subject="Holo3", predicate="scores", numeric_value="80.4", cited_source_id="s0")
    report = await run_citation_agent(
        claims=[claim], retriever_for=lambda c: retr.retrieve, actor_backend="glm_5_2",
        send=number_in_source_send(),
    )
    res = report.results[0]
    assert res.verified is False
    assert res.exhausted is True
    assert res.tag == VerificationTag.U
    assert res.committed_tag == EXHAUSTED_TAG
    assert res.published is True  # committed as a known-unknown, NOT dropped
    surfaced = [c for c in report.could_not_verify if c["bid_key"] == claim.bid_key]
    assert surfaced  # the exhausted claim is surfaced
    assert surfaced[0].get("reasons")  # the surfaced set carries reasons ("WITH reasons", not empty)
    actions = [d.get("action") for d in res.outcome.directives]
    assert "RE_BRANCH" in actions and actions[-1] == "EXHAUSTED_SEARCH"
    assert actions.index("RE_BRANCH") < actions.index("EXHAUSTED_SEARCH")


async def test_firewall_holds():
    retr = FakeRetriever([
        mk_source(0, "states 77.8 not the claim.", SourceKind.PRIMARY),
        mk_source(1, "also 77.8 here.", SourceKind.PRIMARY),
        mk_source(2, "leaderboard 77.8.", SourceKind.PRIMARY),
    ])
    free_text = "the source clearly states 77.8 and never 80.4 anywhere in the body"
    claim = Claim(subject="Holo3", predicate="scores", numeric_value="80.4", cited_source_id="s0")
    await run_citation_gate(
        claim=claim, retrieve=retr.retrieve, actor_backend="glm_5_2",
        send=always_not_entailed_send(free_text),
    )
    valid_codes = {r.value for r in RetryReason}
    assert retr.reqs, "the gate must have called retrieve at least once"
    # a re-branch DID occur (≥1 non-None constraint), so the firewall is actually exercised
    rebranch_reqs = [r for r in retr.reqs if r.constraint is not None]
    assert rebranch_reqs, "the WRONG claim must have produced at least one ERG re-branch"
    for req in retr.reqs:
        if req.constraint is not None:
            # a conforming negative-constraint signal: '[REJECTED: <bid_key> | <tried sources>] ...'
            assert req.constraint.startswith("[REJECTED:")
            assert claim.bid_key in req.constraint  # the bid_key crosses (identity, not free text)
            for prior in (req.tried_sources or ()):
                assert prior in req.constraint  # the tried-source ids cross
        assert req.reason_code is None or req.reason_code in valid_codes
        # the FIREWALL: the critic's free-text reason NEVER crosses the re-branch
        assert free_text not in (req.constraint or "")
        assert req.reason_code != free_text


async def test_entailment_not_citation_exists():
    # the retriever DOES return the cited source (the citation EXISTS) but the source lacks the number.
    retr = FakeRetriever([mk_source(0, "Holo3 reaches 77.8% on OSWorld.", SourceKind.PRIMARY)])
    res = await run_citation_gate(
        claim=Claim(subject="Holo3", predicate="scores", numeric_value="80.4", cited_source_id="s0"),
        retrieve=retr.retrieve, actor_backend="glm_5_2", send=number_in_source_send(),
    )
    assert len(retr.reqs) >= 1  # retrieve was called
    assert any(r is not None for r in retr.returns)  # a Source was actually returned => the citation EXISTS
    assert res.verified is False
    assert res.tag == VerificationTag.U  # entailment != citation-exists


async def test_bare_u_same_family_not_published_revert():
    # actor and critic SAME family => the gate raises EntailmentError internally => a bare-U outcome.
    retr = FakeRetriever([mk_source(0, "x is 1", SourceKind.PRIMARY)])
    claim = Claim(subject="x", predicate="is", numeric_value="1", cited_source_id="s0")
    report = await run_citation_agent(
        claims=[claim], retriever_for=lambda c: retr.retrieve,
        actor_backend="deepseek_v4_flash", critic_backend="deepseek_v4_flash",
        send=number_in_source_send(),
    )
    res = report.results[0]
    assert res.verified is False
    assert res.exhausted is False
    assert res.published is False
    assert report.complete is False
    assert report.decision["decision"] == "REVERT"
    # a bare-U claim is SURFACED with reasons, never silently dropped (symmetric with the exhausted case)
    surfaced = [c for c in report.could_not_verify if c["bid_key"] == claim.bid_key]
    assert surfaced and surfaced[0].get("reasons")
    # the same-family critic is recorded on the CitationResult as NOT decorrelated (the contract surface)
    assert res.decorrelated is False
    assert family_of(res.critic_backend) == family_of("deepseek_v4_flash")


async def test_decorrelation_surfaced():
    retr = FakeRetriever([mk_source(0, "a is 1", SourceKind.PRIMARY)])
    res = await run_citation_gate(
        claim=Claim(subject="a", predicate="is", numeric_value="1", cited_source_id="s0"),
        retrieve=retr.retrieve, actor_backend="glm_5_2", send=number_in_source_send(),
    )
    assert res.decorrelated is True
    assert is_decorrelated("glm_5_2", res.critic_backend)
    assert family_of(res.critic_backend) != family_of("glm_5_2")
    # OD#6 floor-family decorrelation (no model call): qwen_research resolves a cross-family critic
    crit = decorrelated_critic_backend("qwen_research")
    assert is_decorrelated("qwen_research", crit)
    assert family_of(crit) != "qwen"


# ── C) report assembly + Default-FAIL ────────────────────────────────────────

async def test_report_assembly_default_fail():
    true1 = Claim(subject="Holo3", predicate="scores", numeric_value="77.8", cited_source_id="s0")
    vendorc = Claim(subject="device", predicate="draws", numeric_value="55", cited_source_id="s0")
    wrong = Claim(subject="Agent", predicate="scores", numeric_value="80.4", cited_source_id="s0")
    true2 = Claim(subject="Tongyi", predicate="has", numeric_value="30.5", cited_source_id="s0")
    retrievers = {
        true1.bid_key: FakeRetriever([mk_source(0, "Holo3 reaches 77.8% on OSWorld.", SourceKind.PRIMARY)]),
        vendorc.bid_key: FakeRetriever([mk_source(5, "Vendor sheet: device draws 55 watts.", SourceKind.VENDOR)]),
        wrong.bid_key: FakeRetriever([
            mk_source(1, "Agent reaches 69.9%.", SourceKind.PRIMARY),
            mk_source(2, "Agent at 70.1%.", SourceKind.PRIMARY),
            mk_source(3, "Agent at 71%.", SourceKind.PRIMARY),
        ]),
        true2.bid_key: FakeRetriever([mk_source(4, "Tongyi has 30.5B params.", SourceKind.PRIMARY)]),
    }
    report = await run_citation_agent(
        claims=[true1, vendorc, wrong, true2],
        retriever_for=lambda c: retrievers[c.bid_key].retrieve,
        actor_backend="glm_5_2", send=number_in_source_send(),
    )
    assert len(report.published) == 4  # 3 verified (2 PV + 1 VS) + 1 exhausted are all is_complete
    assert any(c["bid_key"] == wrong.bid_key for c in report.could_not_verify)
    assert report.counts["verified"] == 3
    assert report.counts["exhausted"] == 1
    assert report.counts["total"] == 4
    # the per-tag split is pinned (a miscounted PV/VS/U would slip an aggregate-only check)
    assert report.counts["PV"] == 2 and report.counts["VS"] == 1 and report.counts["U"] == 1
    assert [r.claim for r in report.results] == [true1, vendorc, wrong, true2]  # INPUT ORDER preserved
    # all four are is_complete (3 verified + 1 exhausted-tagged) -> the report fast-forwards
    assert report.complete is True
    assert report.decision["decision"] == "FAST_FORWARD"


async def test_empty_claim_set_reverts():
    report = await run_citation_agent(
        claims=[], retriever_for=lambda c: None, actor_backend="glm_5_2", send=number_in_source_send(),
    )
    assert report.decision["decision"] == "REVERT"
    assert report.complete is False  # no vacuous FAST_FORWARD
    assert report.counts["total"] == 0


# ── retriever factory ────────────────────────────────────────────────────────

def test_lks_retriever_factory_binds_query(monkeypatch):
    captured = {}

    def recording_make(**kwargs):
        captured["query"] = kwargs.get("query")

        async def _retrieve(req):
            return None

        return _retrieve

    monkeypatch.setattr("core.research.citation.make_research_retriever", recording_make)

    factory = lks_retriever_factory(query_for=lambda c: f"Q::{c.subject}")
    factory(Claim(subject="Holo3", predicate="x"))
    assert captured["query"] == "Q::Holo3"

    default_factory = lks_retriever_factory()
    claim = Claim(subject="Holo3", predicate="scores", numeric_value="5")
    default_factory(claim)
    assert captured["query"] == claim.render()


# ── D) scoring ───────────────────────────────────────────────────────────────

def test_citation_false_pass_rate_low():
    planted = [
        LabeledItem("t1", {"subject": "a", "predicate": "is", "numeric_value": "5",
                           "source_text": "a is 5", "actor_backend": "glm_5_2"}, Label.TRUE),
        LabeledItem("w1", {"subject": "a", "predicate": "is", "numeric_value": "9",
                           "source_text": "a is 5", "actor_backend": "glm_5_2"}, Label.WRONG),
    ]
    bd = citation_false_pass_rate(planted, send=number_in_source_send(), default_actor_backend="glm_5_2")
    assert bd["false_pass_rate"] == 0.0
    assert bd["n_wrong"] == 1
    assert bd["n_true"] == 1
    assert bd["true_passed"] == 1
    assert bd["wrong_caught"] == 1


# ── cite_draft end-to-end (extract → gate), injected model + retriever + critic ──

async def test_cite_draft_extract_then_gate():
    async def extractor(messages, backend):
        return ('{"claims":[{"subject":"Holo3","predicate":"scores","numeric_value":"77.8",'
                '"cited_source_id":"s0"}]}')

    retr = FakeRetriever([mk_source(0, "Holo3 reaches 77.8% on OSWorld.", SourceKind.PRIMARY)])
    report = await cite_draft(
        draft="Holo3 scores 77.8 on OSWorld.", retriever_for=lambda c: retr.retrieve,
        actor_backend="glm_5_2", model_send=extractor, send=number_in_source_send(),
    )
    # (i) the extractor result flowed into the gate; (ii) the verified PV report fast-forwards
    assert report.counts["total"] == 1
    assert report.results[0].verified is True and report.results[0].tag == VerificationTag.PV
    assert report.decision["decision"] == "FAST_FORWARD"


async def test_cite_draft_extract_backend_override():
    seen = {}

    async def extractor(messages, backend):
        seen["backend"] = backend
        return '{"claims":[{"subject":"a","predicate":"is","numeric_value":"5","cited_source_id":"s0"}]}'

    retr = FakeRetriever([mk_source(0, "a is 5", SourceKind.PRIMARY)])
    await cite_draft(
        draft="a is 5", retriever_for=lambda c: retr.retrieve, actor_backend="glm_5_2",
        extract_backend="extractor_backend", model_send=extractor, send=number_in_source_send(),
    )
    assert seen["backend"] == "extractor_backend"  # the extractor step honors extract_backend, not actor_backend


async def test_cite_draft_empty_extraction_reverts():
    async def empty_extractor(messages, backend):
        return '{"claims":[]}'

    report = await cite_draft(
        draft="no quantitative claims here", retriever_for=lambda c: None,
        actor_backend="glm_5_2", model_send=empty_extractor, send=number_in_source_send(),
    )
    assert report.counts["total"] == 0
    assert report.decision["decision"] == "REVERT"  # no vacuous FAST_FORWARD on an empty extraction


# ── construction validation ──────────────────────────────────────────────────

async def test_construction_validation():
    with pytest.raises(CitationAgentError):
        await run_citation_agent(claims=[], retriever_for=lambda c: None, actor_backend="")
    with pytest.raises(CitationAgentError):
        await run_citation_agent(claims=[], retriever_for=lambda c: None, actor_backend="glm_5_2", concurrency=0)
    with pytest.raises(CitationAgentError):
        await run_citation_agent(claims=[], retriever_for=lambda c: None, actor_backend="glm_5_2", max_attempts=0)
    # cite_draft validates FAIL-FAST (before the extractor LLM call) — an invalid config raises with NO call
    calls = []

    async def _ex(messages, backend):
        calls.append(1)
        return '{"claims":[{"subject":"a","predicate":"is","numeric_value":"1"}]}'

    with pytest.raises(CitationAgentError):
        await cite_draft(draft="a is 1", retriever_for=lambda c: None, actor_backend="", model_send=_ex)
    assert calls == []  # the extractor was NOT called (fail-fast before the wasted model call)
