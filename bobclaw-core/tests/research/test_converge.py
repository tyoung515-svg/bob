"""MS2-R5 — unit tests for the refute-and-vote convergence controller (`core/research/converge.py`).

PURE: the CitationAgent surface (`cite`), the adversarial refuter step (`refute`), and the refuter `send` are
all injected — NO network, NO real model/git/Qdrant. Each case pins CONCRETE behavior and FAILS a weakened
controller.
"""
import json

import pytest

from core.research.converge import (
    ConvergeResult,
    REFUTE_PROMPT_TEMPLATE,
    RefutationResult,
    RefuteVerdict,
    ResearchConvergeError,
    RoundResult,
    build_refute_prompt,
    decorrelated_refuter_backend,
    make_citation_check,
    parse_refute_verdict,
    refute_claim,
    run_refute_and_vote,
)
from core.verify.entailment import Claim
from core.verify.postcondition import family_of, is_decorrelated


# ── Test helpers ─────────────────────────────────────────────────────────────

def mk_claim(subj, pred, num):
    return Claim(subject=subj, predicate=pred, numeric_value=str(num), text=f"{subj} {pred} {num}")


class FakeCiteResult:
    """A CitationReport-like object the controller consumes (.results / .could_not_verify / .as_dict)."""

    def __init__(self, verified_keys, cnv=None):
        self._v = set(verified_keys)
        self.could_not_verify = list(cnv or [])
        all_keys = set(verified_keys)
        for kv in self.could_not_verify:
            if "bid_key" in kv:
                all_keys.add(kv["bid_key"])
        self.results = [type("R", (), {"bid_key": k, "verified": (k in self._v)})() for k in all_keys]

    def as_dict(self):
        return {"verified": sorted(self._v)}


def make_fake_cite(verified_by_key):
    """An async cite: a claim is verified unless verified_by_key maps its bid_key to False (default True)."""

    async def _cite(claims):
        keys = [c.bid_key for c in claims]
        verified = [k for k in keys if verified_by_key.get(k, True)]
        cnv = [{"bid_key": k, "reasons": ["planted-unverified"]} for k in keys if k not in verified]
        return FakeCiteResult(set(verified), cnv)

    return _cite


def make_fake_refute(refute_keys):
    """An async refute: refute claims whose bid_key is in refute_keys, uphold the rest."""

    async def _refute(*, claim, evidence, asserter_backend, refuter_backend, team=None, send=None):
        v = RefuteVerdict.REFUTED if claim.bid_key in refute_keys else RefuteVerdict.UPHELD
        return RefutationResult(
            bid_key=claim.bid_key, verdict=v, refuted=(v is RefuteVerdict.REFUTED),
            reasons=("planted-refutation",) if v is RefuteVerdict.REFUTED else ("upheld",),
            refuter_backend=refuter_backend, decorrelated=is_decorrelated(asserter_backend, refuter_backend),
        )

    return _refute


def round_aware_refuter(refute_plan):
    """An async refute that refutes a DIFFERENT set each round. `refute_plan` is a list of bid_key-sets (one per
    round). The round advances when a claim is SEEN A SECOND TIME (a surviving claim re-appears in the next
    round) — the controller re-attacks survivors each round, so the first survivor of a new round triggers the
    advance. Deterministic under asyncio.gather (the fake has no inner await, so state updates don't interleave)."""
    state = {"round": 0, "seen": set(), "plans": [set(s) for s in refute_plan]}

    async def _refute(*, claim, evidence, asserter_backend, refuter_backend, team=None, send=None):
        k = claim.bid_key
        if k in state["seen"]:  # a claim re-appears -> a new round has begun
            state["round"] += 1
            state["seen"] = set()
        state["seen"].add(k)
        rnd = state["round"]
        keys = state["plans"][rnd] if rnd < len(state["plans"]) else set()
        v = RefuteVerdict.REFUTED if k in keys else RefuteVerdict.UPHELD
        return RefutationResult(
            bid_key=k, verdict=v, refuted=(v is RefuteVerdict.REFUTED),
            reasons=("planted-refutation",) if v is RefuteVerdict.REFUTED else ("upheld",),
            refuter_backend=refuter_backend, decorrelated=is_decorrelated(asserter_backend, refuter_backend),
        )

    return _refute


def send_refute(verdict):
    """An async refuter `send` returning canned JSON with the given verdict."""

    async def _send(messages, backend):
        return json.dumps({"verdict": verdict, "reasons": ["r"]})

    return _send


# ── 1. parse tolerance ───────────────────────────────────────────────────────

async def test_parse_refute_verdict_tolerant():
    v, r = parse_refute_verdict('{"verdict":"refuted","reasons":["x"]}')
    assert v is RefuteVerdict.REFUTED and r == ["x"]

    v2, r2 = parse_refute_verdict('```json\n{"verdict":"upheld","reasons":["y"]}\n```')
    assert v2 is RefuteVerdict.UPHELD and r2 == ["y"]

    # JSON-in-prose: the LAST verdict-bearing balanced object wins
    v3, _ = parse_refute_verdict('noise {"verdict":"upheld"} more {"verdict":"refuted"} tail')
    assert v3 is RefuteVerdict.REFUTED

    # a `}` inside a JSON string must NOT break depth detection
    v4, r4 = parse_refute_verdict('{"verdict":"upheld","reasons":["see } here"]}')
    assert v4 is RefuteVerdict.UPHELD and r4 == ["see } here"]

    # case-insensitive verdict
    v5, _ = parse_refute_verdict('{"verdict":"REFUTED"}')
    assert v5 is RefuteVerdict.REFUTED

    # no JSON -> UNKNOWN + parse_error; never raises (incl. empty / None)
    v6, r6 = parse_refute_verdict("completely random text")
    assert v6 is RefuteVerdict.UNKNOWN and any("parse_error" in x for x in r6)
    assert parse_refute_verdict("")[0] is RefuteVerdict.UNKNOWN
    assert parse_refute_verdict(None)[0] is RefuteVerdict.UNKNOWN


# ── 2. refuter resolution — 3-family + fail CLOSED ────────────────────────────

def test_decorrelated_refuter_3family_and_failclosed(monkeypatch):
    r = decorrelated_refuter_backend("qwen_research", critic_backend="deepseek_v4_flash")
    assert family_of(r) not in ("qwen", "deepseek")  # a THIRD family
    assert is_decorrelated("qwen_research", r)

    r2 = decorrelated_refuter_backend("qwen_research")
    assert is_decorrelated("qwen_research", r2)

    # fail CLOSED: force EVERY pool (the refuter pool AND the postcondition fallback pool the resolver delegates
    # to) to same-family-only, so no candidate is cross-family from the asserter -> ResearchConvergeError (never
    # a silent same-family refuter). Patching only the refuter pool would still let the fallback find a
    # cross-family critic from postcondition's own 6-family default.
    monkeypatch.setattr("core.research.converge.DEFAULT_REFUTER_PREFERENCE", ("deepseek_v4_flash",))
    monkeypatch.setattr("core.verify.postcondition.DEFAULT_CRITIC_PREFERENCE", ("deepseek_v4_flash",))
    with pytest.raises(ResearchConvergeError):
        decorrelated_refuter_backend("deepseek_v4_flash", candidates=["deepseek_v4_flash"])


# ── 3. refute_claim — cross-family enforced + fail-safe ───────────────────────

async def test_refute_claim_enforces_crossfamily_and_failsafe():
    claim = mk_claim("a", "is", 1)
    # SAME family -> fail CLOSED
    with pytest.raises(ResearchConvergeError):
        await refute_claim(claim=claim, asserter_backend="deepseek_v4_flash",
                           refuter_backend="deepseek_v4_flash", send=send_refute("upheld"))

    # cross-family refuter returning "refuted"
    res = await refute_claim(claim=claim, asserter_backend="deepseek_v4_flash",
                             refuter_backend="minimax", send=send_refute("refuted"))
    assert res.refuted is True and res.decorrelated is True and res.verdict is RefuteVerdict.REFUTED

    # an unreachable refuter is NOT a refutation (fail-safe -> UNKNOWN, refuted False)
    async def failing_send(*a, **k):
        raise RuntimeError("down")

    res2 = await refute_claim(claim=claim, asserter_backend="deepseek_v4_flash",
                              refuter_backend="minimax", send=failing_send)
    assert res2.verdict is RefuteVerdict.UNKNOWN and res2.refuted is False


# ── 4. a planted CONTESTED claim is refuted -> could_not_verify ───────────────

async def test_planted_contested_claim_refuted_to_cnv():
    robust = mk_claim("Holo3", "scores", 77.8)
    contested = mk_claim("Holo3", "scores", 80.4)
    res = await run_refute_and_vote(
        claims=[robust, contested],
        cite=make_fake_cite({robust.bid_key: True, contested.bid_key: True}),  # citation PASSES both
        refute=make_fake_refute({contested.bid_key}),                          # the refuter knocks out contested
        asserter_backend="qwen_research", critic_backend="deepseek_v4_flash", refuter_backend="minimax",
    )
    assert contested.bid_key not in res.surviving_keys
    assert robust.bid_key in res.surviving_keys
    cnv = [c for c in res.could_not_verify if c["bid_key"] == contested.bid_key]
    assert len(cnv) == 1 and cnv[0]["kind"] == "refuted" and "planted-refutation" in cnv[0]["reasons"]


# ── 5. a citation-unverified claim -> could_not_verify (kind unverified) ──────

async def test_citation_unverified_to_cnv():
    weak = mk_claim("X", "is", 5)
    res = await run_refute_and_vote(
        claims=[weak], cite=make_fake_cite({weak.bid_key: False}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", refuter_backend="minimax",
    )
    assert weak.bid_key not in res.surviving_keys
    cnv = [c for c in res.could_not_verify if c["bid_key"] == weak.bid_key]
    assert len(cnv) == 1 and cnv[0]["kind"] == "unverified" and "planted-unverified" in cnv[0]["reasons"]
    assert res.decision["decision"] == "REVERT" and res.complete is False


# ── 6. a ROBUST claim survives N rounds + no-delta convergence ────────────────

async def test_robust_survives_and_nodelta_convergence():
    robust = mk_claim("R", "is", 1)
    c1 = mk_claim("C1", "is", 2)
    c2 = mk_claim("C2", "is", 3)
    refute_fake = round_aware_refuter([{c1.bid_key}, {c2.bid_key}])  # r0 refutes c1, r1 refutes c2, robust upheld
    res = await run_refute_and_vote(
        claims=[robust, c1, c2], cite=make_fake_cite({}), refute=refute_fake,
        asserter_backend="qwen_research", refuter_backend="minimax", max_rounds=4,
    )
    assert robust.bid_key in res.surviving_keys
    assert all(robust.bid_key in rr.survivors_after for rr in res.round_results)
    cnv_keys = {c["bid_key"] for c in res.could_not_verify}
    assert c1.bid_key in cnv_keys and c2.bid_key in cnv_keys
    assert res.converged_reason.startswith("no-delta")
    # deterministic given the plan: r0 refutes c1, r1 refutes c2, r2 upholds all -> {robust}=={robust} no-delta
    assert res.rounds == 3


# ── 7. convergence fires on claim-set STABILITY (no-delta, 1 round) ───────────

async def test_stability_converges_one_round():
    a, b = mk_claim("a", "is", 1), mk_claim("b", "is", 2)
    res = await run_refute_and_vote(
        claims=[a, b], cite=make_fake_cite({}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", refuter_backend="minimax",
    )
    assert res.rounds == 1 and res.converged_reason.startswith("no-delta")
    assert set(res.surviving_keys) == {a.bid_key, b.bid_key}
    assert res.decision["decision"] == "FAST_FORWARD" and res.complete is True


# ── 8. convergence fires on the ROUND CAP ─────────────────────────────────────

async def test_round_cap_convergence():
    a, b, c = mk_claim("a", "is", 1), mk_claim("b", "is", 2), mk_claim("c", "is", 3)
    # each round refutes a DIFFERENT single claim (the set shrinks but is never stable within the cap)
    refute_fake = round_aware_refuter([{a.bid_key}, {b.bid_key}, {c.bid_key}])
    res = await run_refute_and_vote(
        claims=[a, b, c], cite=make_fake_cite({}), refute=refute_fake,
        asserter_backend="qwen_research", refuter_backend="minimax", max_rounds=2,
    )
    assert res.rounds == 2 and res.converged_reason.startswith("round cap") and res.converged is True


# ── 9. convergence fires on the BUDGET ceiling (the MS-4 cost bound) ──────────

async def test_budget_bound():
    a = mk_claim("a", "is", 1)
    res = await run_refute_and_vote(
        claims=[a], cite=make_fake_cite({}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", refuter_backend="minimax", max_usd=0.05, round_usd=0.10,
    )
    assert res.budget_bound is True and res.converged_reason.startswith("budget")
    assert res.rounds == 0
    # Default-FAIL: a zero-round budget bind never verified the input -> it is NOT published as surviving; it is
    # surfaced as could-not-verify and the run ESCALATEs (contested-by-cost), never a vacuous FAST_FORWARD.
    assert set(res.surviving_keys) == set()
    assert a.bid_key in {c["bid_key"] for c in res.could_not_verify}
    assert res.decision["decision"] == "ESCALATE" and res.complete is False

    res2 = await run_refute_and_vote(
        claims=[a], cite=make_fake_cite({}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", refuter_backend="minimax", max_usd=1.0, round_usd=0.10,
    )
    assert res2.budget_bound is False and res2.rounds >= 1  # the ceiling is the discriminator, not a constant
    assert res2.decision["decision"] == "FAST_FORWARD"  # the verified survivor publishes when budget allows a round


# ── 10. empty input / all-refuted -> REVERT ───────────────────────────────────

async def test_empty_and_all_refuted_revert():
    res = await run_refute_and_vote(
        claims=[], cite=make_fake_cite({}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", refuter_backend="minimax",
    )
    assert res.surviving == () and res.complete is False
    assert res.decision["decision"] == "REVERT" and res.converged_reason == "empty surviving set"

    a = mk_claim("x", "y", 1)
    res2 = await run_refute_and_vote(
        claims=[a], cite=make_fake_cite({}), refute=make_fake_refute({a.bid_key}),
        asserter_backend="qwen_research", refuter_backend="minimax",
    )
    assert res2.surviving == () and res2.decision["decision"] == "REVERT"
    assert a.bid_key in {c["bid_key"] for c in res2.could_not_verify}


# ── 11. the 3-family + decorrelated proof is surfaced ─────────────────────────

async def test_three_family_and_decorrelated_surfaced():
    a = mk_claim("a", "is", 1)
    res = await run_refute_and_vote(
        claims=[a], cite=make_fake_cite({}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", critic_backend="deepseek_v4_flash", refuter_backend="minimax",
    )
    assert res.decorrelated is True and is_decorrelated("qwen_research", res.refuter_backend)
    assert res.three_family is True

    res2 = await run_refute_and_vote(
        claims=[a], cite=make_fake_cite({}), refute=make_fake_refute(set()),
        asserter_backend="qwen_research", critic_backend=None, refuter_backend="minimax",
    )
    assert res2.three_family is False and res2.decorrelated is True


# ── 12. could_not_verify carries reasons + kind + round ───────────────────────

async def test_cnv_carries_reasons_kind_round():
    refuted_claim = mk_claim("R", "v", 1)
    unver_claim = mk_claim("U", "v", 2)
    res = await run_refute_and_vote(
        claims=[refuted_claim, unver_claim],
        cite=make_fake_cite({refuted_claim.bid_key: True, unver_claim.bid_key: False}),
        refute=make_fake_refute({refuted_claim.bid_key}),
        asserter_backend="qwen_research", refuter_backend="minimax",
    )
    assert len(res.could_not_verify) == 2
    for item in res.could_not_verify:
        assert item["bid_key"] and item["reasons"] and len(item["reasons"]) > 0
        assert item["kind"] in ("refuted", "unverified")
        assert isinstance(item["round"], int)


# ── 13. make_citation_check bridges R4 ────────────────────────────────────────

async def test_make_citation_check_bridges_r4(monkeypatch):
    sentinel = object()
    seen = {}

    async def fake_run_citation_agent(*, claims, retriever_for, actor_backend, team=None,
                                      critic_backend=None, send=None, max_attempts=8, concurrency=4):
        seen["claims"] = claims
        seen["actor_backend"] = actor_backend
        return sentinel

    # run_citation_agent is imported LAZILY from core.research.citation inside _cite -> patch the SOURCE module.
    monkeypatch.setattr("core.research.citation.run_citation_agent", fake_run_citation_agent)
    cite = make_citation_check(retriever_for=lambda c: None, actor_backend="glm_5_2")
    claim = mk_claim("a", "is", 1)
    out = await cite([claim])
    assert out is sentinel and seen["claims"] == [claim] and seen["actor_backend"] == "glm_5_2"


# ── 14. construction validation ───────────────────────────────────────────────

async def test_construction_validation():
    a = mk_claim("a", "is", 1)
    cite_fake = make_fake_cite({})
    refute_fake = make_fake_refute(set())
    base = dict(claims=[a], cite=cite_fake, refute=refute_fake,
                asserter_backend="qwen_research", refuter_backend="minimax")

    with pytest.raises(ResearchConvergeError):
        await run_refute_and_vote(**{**base, "asserter_backend": ""})
    with pytest.raises(ResearchConvergeError):
        await run_refute_and_vote(**{**base, "max_rounds": 0})
    with pytest.raises(ResearchConvergeError):
        await run_refute_and_vote(**{**base, "max_usd": 0})
    with pytest.raises(ResearchConvergeError):
        await run_refute_and_vote(**{**base, "round_usd": 0})
    with pytest.raises(ResearchConvergeError):
        await run_refute_and_vote(**{**base, "cite": None})
    # run_refute_and_vote's OWN same-family refuter override guard (a code path separate from refute_claim's):
    # an explicit refuter_backend same-family as the asserter fails CLOSED before any round runs.
    with pytest.raises(ResearchConvergeError):
        await run_refute_and_vote(**{**base, "refuter_backend": "qwen_research"})  # asserter is qwen_research


# ── 15. debate.py untouched (reuse is a MIRROR, not a shared import) ──────────

def test_debate_untouched_smoke():
    import core.nodes.debate as _d
    assert hasattr(_d, "debate_converge_node")

    import core.research.converge as _cv
    with open(_cv.__file__, "r", encoding="utf-8") as f:
        text = f.read()
    assert "core.nodes.debate" not in text  # the pattern is MIRRORED, never imported
    assert "import debate" not in text
    # the Default-FAIL gate + decorrelation are IMPORTED (reused), never re-implemented (CONTRACTS-R5 obl. 12):
    # a controller that re-rolled an identical FAST_FORWARD/REVERT/ESCALATE tree would pass every behavior
    # assertion, so pin the SOURCE — is_complete/termination_decision from MS-3, decorrelation from MS-2.
    assert "from core.verify.termination import" in text
    assert "from core.verify.postcondition import" in text


async def test_results_stable_deterministic_order():
    # CONTRACTS-R5 obl.12 "results in a stable order": survivors/could_not_verify/round_results preserve INPUT
    # ORDER and are deterministic across identical runs (the controller stores survivors in a LIST, never a set —
    # a set-backed store would shuffle order and silently break the round_aware_refuter fixture's determinism).
    a, b, c, d = mk_claim("a", "is", 1), mk_claim("b", "is", 2), mk_claim("c", "is", 3), mk_claim("d", "is", 4)
    kwargs = dict(cite=make_fake_cite({}), refute=make_fake_refute({b.bid_key, d.bid_key}),
                  asserter_backend="qwen_research", refuter_backend="minimax")
    r1 = await run_refute_and_vote(claims=[a, b, c, d], **kwargs)
    r2 = await run_refute_and_vote(claims=[a, b, c, d], **kwargs)
    # survivors keep INPUT ORDER (a, c) — NOT a set-shuffled order
    assert list(r1.surviving_keys) == [a.bid_key, c.bid_key]
    assert list(r1.round_results[0].survivors_after) == [a.bid_key, c.bid_key]
    # could_not_verify preserves INPUT ORDER too ([b, d]) — not merely cross-run determinism (a sorted or
    # refuted-before-unverified re-grouping would pass the cross-run check but fail this)
    assert [it["bid_key"] for it in r1.could_not_verify] == [b.bid_key, d.bid_key]
    # deterministic across identical runs
    assert list(r1.surviving_keys) == list(r2.surviving_keys)
    assert [it["bid_key"] for it in r1.could_not_verify] == [it["bid_key"] for it in r2.could_not_verify]
    assert [rr.round_idx for rr in r1.round_results] == [rr.round_idx for rr in r2.round_results]


def test_build_refute_prompt_brace_safe():
    # a claim/evidence with literal { } must not break rendering or inject a placeholder
    p = build_refute_prompt("model{x} scores", "77.8", "evidence with {braces} and {numeric_value}")
    assert "77.8" in p and "{braces}" in p and "model{x} scores" in p
    # the template must carry all three NAMED placeholders (a degraded template fails this, not a bare non-empty)
    for ph in ("{claim_text}", "{numeric_value}", "{evidence}"):
        assert ph in REFUTE_PROMPT_TEMPLATE
    # single-pass brace safety: a literal {numeric_value} INSIDE the evidence is NOT re-substituted to 77.8
    assert "and {numeric_value}" in p
