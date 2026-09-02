"""MS#4 · E1 earning layer — Slice 0 tests (schema invariants + triage + grant→Scope seam).

Exercises the deterministic contract lifecycle end of Slice 0: I1 (distinct critic seat),
I2 (justified risk), I7 (no-AI auto-reject in triage), and the green integration against
core/permissions.py (ratify → to_grant → grant_to_scope → evaluate_action).
"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.earn import (
    AIPolicy,
    BugBountyProfile,
    CriticStance,
    CriticVerdict,
    PayoffType,
    ProposedContract,
    Provenance,
    RiskDisclosure,
    RiskItem,
    RiskLevel,
    ScopeClause,
    Terms,
    TriageBucket,
    grant_to_scope,
)
from core.permissions import evaluate_action

_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


def _risk(level=RiskLevel.LOW):
    return RiskItem(level=level, justification="grounded in program scope")


def _bounty(**over):
    """A valid bug-bounty contract (portfolio payoff ⇒ not ROI-rejected unless overridden)."""
    kw = dict(
        contract_id="ct-t-1",
        proposer_seat="bob.proposer.glm",
        proposed_at=_NOW,
        scope=ScopeClause(
            targets=["immunefi:acme-vault"],
            in_scope_actions=["static review of in-scope contracts", "fork-test poc"],
            out_of_scope=["mainnet interaction"],
            authorization_basis="program scope + terms snapshot",
            ai_policy=over.pop("ai_policy", AIPolicy.ALLOWED),
        ),
        terms=Terms(
            burn_budget_usd=8.0,
            payoff_type=PayoffType.PORTFOLIO,
            expected_payoff_usd=None,
            payout_timeline="portfolio",
            deadline=None,
        ),
        risk=RiskDisclosure(
            authorization_certainty=_risk(),
            duplicate_likelihood=_risk(),
            reputational_exposure=_risk(),
            claim_confidence=0.4,
            claim_confidence_basis="candidate path, not yet PoC-confirmed",
            known_unknowns="needs fork test",
        ),
        provenance=Provenance(
            source_urls=["https://immunefi.com/bounty/acme/"],
            terms_snapshot_sha256="0" * 64,
            fetched_at=_NOW,
        ),
        profile=BugBountyProfile(
            platform="immunefi", program_handle="acme-vault",
            rules_of_engagement="poc on fork only", poc_required=True,
        ),
    )
    kw.update(over)
    return ProposedContract(**kw)


# ── invariants ───────────────────────────────────────────────────────────────

def test_i2_risk_requires_justification():
    with pytest.raises(ValidationError):
        RiskItem(level=RiskLevel.LOW, justification="")     # min_length=1


def test_i1_critic_seat_must_differ_from_proposer():
    with pytest.raises(ValidationError):                      # model_validator wraps as ValidationError
        _bounty(critic=CriticVerdict(
            critic_seat="bob.proposer.glm",                  # == proposer_seat
            stance=CriticStance.PASS, reviewed_at=_NOW,
        ))


# ── triage (I7 no-AI auto-reject; deterministic, no model) ───────────────────

def test_i7_banned_ai_policy_auto_rejects():
    c = _bounty(ai_policy=AIPolicy.BANNED)
    assert c.run_triage() is TriageBucket.AUTO_REJECT
    assert any("prohibits AI" in r for r in c.auto_reject_reasons)


def test_triage_clean_bounty_not_rejected():
    assert _bounty().run_triage() in (TriageBucket.FAST_APPROVE, TriageBucket.NEEDS_REVIEW)


# ── the green integration: ratify → to_grant → grant_to_scope → evaluate_action ─

def test_to_grant_requires_ratification():
    with pytest.raises(ValueError):
        _bounty().to_grant()                                 # not ratified yet


def test_grant_to_scope_gates_actions():
    c = _bounty()
    c.run_triage()
    c.ratify(ratifier="travis")                              # freeze the envelope
    scope = grant_to_scope(c.to_grant())
    assert evaluate_action("static review of in-scope contracts", scope) == "auto"
    assert evaluate_action("mainnet interaction", scope) == "gate"   # out-of-scope → fail-closed
    assert evaluate_action("file_delete", scope) == "human"          # always-human floor
    assert evaluate_action("", scope) == "human"                     # empty → human
