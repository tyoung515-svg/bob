"""MS#4 · E1 earning layer — Slice 3 tests (JOAT seat wiring · decay sweep · operator surface).

Deterministic: lane seat resolution (proposer via JOAT team / explicit, critic family-diverged),
the decay/cleanup sweep, and the operator ratify/modify/reject adapter.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.earn import CriticConfigError
from core.earn.contract import ContractStatus, Lane, PayoffType, ScopeClause, Terms
from core.earn.critic_config import resolve_lane_seats
from core.earn.lifecycle import OperatorSurface, is_decayed, sweep_decayed

_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


# ── JOAT seat wiring (D-1) ───────────────────────────────────────────────────

def test_resolve_lane_seats_explicit_proposer():
    proposer, critic = resolve_lane_seats(Lane.BUG_BOUNTY, proposer_backend="deepseek_v4_flash")
    assert proposer == "deepseek_v4_flash" and critic == "minimax"


def test_resolve_lane_seats_rejects_same_family():
    with pytest.raises(CriticConfigError):        # proposer minimax vs bounty critic minimax
        resolve_lane_seats(Lane.BUG_BOUNTY, proposer_backend="minimax")


def test_resolve_lane_seats_from_team():
    # demo-fleet worker = deepseek_v4_flash; bounty critic = minimax (divergent) → resolves
    proposer, critic = resolve_lane_seats(Lane.BUG_BOUNTY, team="demo-fleet")
    assert proposer == "deepseek_v4_flash" and critic == "minimax"


def test_resolve_lane_seats_needs_a_proposer():
    with pytest.raises(CriticConfigError):
        resolve_lane_seats(Lane.BUG_BOUNTY)       # no team, no explicit proposer


# ── decay / cleanup sweep ────────────────────────────────────────────────────

def test_sweep_decayed_closes_expired(bounty):
    live = bounty()
    live.ratify(ratifier="t")                     # deadline None → never decays
    expired = bounty()
    expired.ratify(ratifier="t", terms=Terms(
        burn_budget_usd=8.0, payoff_type=PayoffType.PORTFOLIO,
        payout_timeline="p", deadline=_NOW - timedelta(days=1)))
    swept = sweep_decayed([live, expired], now=_NOW)
    assert swept == [expired.contract_id]
    assert expired.status is ContractStatus.CLOSED
    assert live.status is not ContractStatus.CLOSED
    # idempotent: a second sweep closes nothing new
    assert sweep_decayed([live, expired], now=_NOW) == []


def test_is_decayed(bounty):
    c = bounty()
    c.ratify(ratifier="t", terms=Terms(burn_budget_usd=8.0, payoff_type=PayoffType.PORTFOLIO,
                                       payout_timeline="p", deadline=_NOW - timedelta(hours=1)))
    assert is_decayed(c, _NOW) is True
    assert is_decayed(bounty(), _NOW) is False     # no deadline


# ── operator surface (ratify / modify / reject) ──────────────────────────────

def test_operator_ratify_and_reject(bounty):
    c = bounty()
    op = OperatorSurface(c)
    digest = op.ratify("travis")
    assert digest and c.ratified_envelope_sha256 == digest
    assert c.status is ContractStatus.RATIFIED

    c2 = bounty()
    OperatorSurface(c2).reject("travis", reason="out of appetite")
    assert c2.status is ContractStatus.REJECTED


def test_operator_modify_is_binding(bounty):
    c = bounty()
    tightened = ScopeClause(
        targets=["immunefi:acme-vault"], in_scope_actions=["static review of in-scope contracts"],
        out_of_scope=["mainnet interaction", "fork-test poc"],  # operator removes an action
        authorization_basis="tightened by operator", ai_policy=c.scope.ai_policy,
    )
    OperatorSurface(c).modify("travis", scope=tightened)
    assert c.status is ContractStatus.MODIFIED_RATIFIED
    assert c.ratified_scope.out_of_scope == ["mainnet interaction", "fork-test poc"]
