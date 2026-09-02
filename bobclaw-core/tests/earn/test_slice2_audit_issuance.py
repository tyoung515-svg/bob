"""MS#4 · E1 earning layer — Slice 2 tests (audit hook D-3 · FM-2 issuance re-check).

Deterministic set-membership audit against the ratified envelope (bounty + dataset lanes),
and issuance that hard-denies on decay, TOCTOU snapshot drift, or unverifiable provenance.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.earn import IssuanceError, audit_action, issue_grant
from core.earn.contract import PayoffType, Terms

_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


# ── D-3: deterministic audit against the ratified envelope ───────────────────

def test_audit_requires_ratification(bounty):
    with pytest.raises(ValueError):
        audit_action(bounty(), "static review of in-scope contracts", "immunefi:acme-vault")


def test_bounty_audit_authorizes_in_scope(bounty):
    c = bounty(); c.ratify(ratifier="t")
    v = audit_action(c, "static review of in-scope contracts", "immunefi:acme-vault")
    assert v.authorized is True and v.event.action_hash


def test_bounty_audit_denies_out_of_scope_and_unlisted(bounty):
    c = bounty(); c.ratify(ratifier="t")
    assert audit_action(c, "static review of in-scope contracts", "mainnet interaction").authorized is False
    assert audit_action(c, "delete prod db", "immunefi:acme-vault").authorized is False   # action not in scope
    assert audit_action(c, "static review of in-scope contracts", "other:thing").authorized is False


def test_dataset_audit_license_and_pii(dataset):
    c = dataset(); c.ratify(ratifier="t")
    assert audit_action(c, "ingest source", "kaggle:acme-corpus", source_license="CC-BY-4.0").authorized is True
    assert audit_action(c, "ingest source", "kaggle:acme-corpus", source_license="proprietary").authorized is False
    assert audit_action(c, "ingest source", "kaggle:acme-corpus", source_license="CC-BY-4.0", is_pii=True).authorized is False


# ── FM-2 / I3: issuance-time TOCTOU + decay ──────────────────────────────────

def test_issue_requires_ratification(bounty):
    with pytest.raises(IssuanceError):
        issue_grant(bounty(), now=_NOW, fetcher=lambda urls: "x")


def test_issue_refuses_without_fetcher(bounty):
    c = bounty(); c.ratify(ratifier="t")
    with pytest.raises(IssuanceError):          # unverified provenance ⇒ needs_review
        issue_grant(c, now=_NOW, fetcher=None)


def test_issue_hard_denies_toctou(bounty):
    c = bounty(); c.ratify(ratifier="t")
    # the bounty fixture's terms_snapshot_sha256 is "0"*64; a fresh fetch returning anything else = drift
    with pytest.raises(IssuanceError):
        issue_grant(c, now=_NOW, fetcher=lambda urls: "f" * 64)


def test_issue_succeeds_on_match(bounty):
    c = bounty(); c.ratify(ratifier="t")
    grant = issue_grant(c, now=_NOW, fetcher=lambda urls: "0" * 64)   # matches stored snapshot
    assert grant["grant_id"] == f"grant::{c.contract_id}"


def test_issue_denies_decayed(bounty):
    past = Terms(burn_budget_usd=8.0, payoff_type=PayoffType.PORTFOLIO,
                 payout_timeline="portfolio", deadline=_NOW - timedelta(days=1))
    c = bounty()
    c.ratify(ratifier="t", terms=past)          # ratified with a past deadline → decayed
    with pytest.raises(IssuanceError):
        issue_grant(c, now=_NOW, fetcher=lambda urls: "0" * 64)
