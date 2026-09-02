"""MS#4 · E1 earning layer — Slice 4: full-lifecycle E2E + mechanical I1–I8 verification.

Drives propose → critique → triage → ratify → issue → audit → close on a synthetic BOUNTY and
a synthetic DATASET contract using only the shipped Slice 0–3 pieces, and asserts each of the
eight invariants I1–I8 is enforced by a mechanism (not by trust). No model in any gate.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core.earn import (
    ContractStatus,
    CriticStance,
    CriticVerdict,
    IssuanceError,
    OperatorSurface,
    RiskItem,
    RiskLevel,
    TriageBucket,
    audit_action,
    evaluate_poc,
    issue_grant,
    resolve_lane_seats,
    sweep_decayed,
)
from core.earn.contract import Lane, ScopeClause
from core.verify.entailment import EntailmentVerdict

_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


# ── full lifecycle E2E: bounty ───────────────────────────────────────────────

def test_bounty_lifecycle_end_to_end(bounty):
    # propose (PoC reproduced via the FM-1 compound gate) → triage → ratify → issue → audit
    poc = evaluate_poc(EntailmentVerdict.ENTAILED, "high", "high")           # I5 compound gate
    c = bounty(poc_reproduced=poc)
    assert c.run_triage() is TriageBucket.FAST_APPROVE                        # clean, reproduced

    OperatorSurface(c).ratify("travis")                                       # human write
    assert c.status is ContractStatus.RATIFIED

    grant = issue_grant(c, now=_NOW, fetcher=lambda urls: "0" * 64)           # I3/I8 re-verify
    assert grant["grant_id"] == f"grant::{c.contract_id}"

    ok = audit_action(c, "static review of in-scope contracts", "immunefi:acme-vault")
    assert ok.authorized is True
    bad = audit_action(c, "static review of in-scope contracts", "mainnet interaction")
    assert bad.authorized is False                                            # I6 hard boundary

    assert sweep_decayed([c], now=_NOW) == []                                 # no deadline → not decayed


# ── full lifecycle E2E: dataset ──────────────────────────────────────────────

def test_dataset_lifecycle_end_to_end(dataset):
    c = dataset()
    assert c.run_triage() is TriageBucket.FAST_APPROVE                        # licensed, no PII, portfolio
    OperatorSurface(c).ratify("travis")
    grant = issue_grant(c, now=_NOW, fetcher=lambda urls: "a" * 64)
    assert grant["scope"]["deny"] == ["scrape non-listed sources"]
    assert audit_action(c, "ingest source", "kaggle:acme-corpus", source_license="CC-BY-4.0").authorized
    assert not audit_action(c, "ingest source", "kaggle:acme-corpus", source_license="proprietary").authorized


# ── the invariant table I1–I8, each verified by a mechanism ───────────────────

def test_i1_distinct_critic_and_family(bounty):
    # schema: critic_seat != proposer_seat
    with pytest.raises(ValidationError):
        bounty(critic=CriticVerdict(critic_seat="bob.proposer.glm", stance=CriticStance.PASS,
                                    reviewed_at=_NOW))
    # config: critic family ⊥ proposer family
    _, critic = resolve_lane_seats(Lane.BUG_BOUNTY, proposer_backend="deepseek_v4_flash")
    assert critic == "minimax"


def test_i2_justified_risk():
    with pytest.raises(ValidationError):
        RiskItem(level=RiskLevel.LOW, justification="")


def test_i3_i8_audit_and_issuance(bounty):
    c = bounty(); c.ratify(ratifier="t")
    # I8: no fetcher ⇒ won't issue on the proposer's self-attested snapshot
    with pytest.raises(IssuanceError):
        issue_grant(c, now=_NOW, fetcher=None)
    # I3: snapshot drift at issuance ⇒ hard-deny
    with pytest.raises(IssuanceError):
        issue_grant(c, now=_NOW, fetcher=lambda urls: "f" * 64)


def test_i4_binding_modifications(bounty):
    c = bounty()
    tightened = ScopeClause(targets=["immunefi:acme-vault"],
                            in_scope_actions=["static review of in-scope contracts"],
                            out_of_scope=["mainnet interaction", "fork-test poc"],
                            authorization_basis="operator-tightened", ai_policy=c.scope.ai_policy)
    OperatorSurface(c).modify("travis", scope=tightened)
    assert c.ratified_scope.out_of_scope == ["mainnet interaction", "fork-test poc"]  # operator's version

def test_i5_poc_compound_gate():
    assert evaluate_poc(EntailmentVerdict.ENTAILED, "high", "high") is True
    assert evaluate_poc(EntailmentVerdict.ENTAILED, "high", "low") is False   # observed != claimed


def test_i6_scope_is_legal_boundary(bounty):
    c = bounty(); c.ratify(ratifier="t")
    assert audit_action(c, "static review of in-scope contracts", "mainnet interaction").authorized is False


def test_i7_no_ai_auto_reject(bounty):
    from core.earn.contract import AIPolicy
    assert bounty(ai_policy=AIPolicy.BANNED).run_triage() is TriageBucket.AUTO_REJECT


def test_decay_closes_expired_in_lifecycle(bounty):
    from core.earn.contract import PayoffType, Terms
    c = bounty()
    c.ratify(ratifier="t", terms=Terms(burn_budget_usd=8.0, payoff_type=PayoffType.PORTFOLIO,
                                       payout_timeline="p", deadline=_NOW - timedelta(days=2)))
    assert sweep_decayed([c], now=_NOW) == [c.contract_id]
    assert c.status is ContractStatus.CLOSED
