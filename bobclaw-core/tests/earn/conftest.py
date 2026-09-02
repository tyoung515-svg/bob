"""Shared fixtures for the E1 earning-layer tests."""
from datetime import datetime, timezone

import pytest

from core.earn.contract import (
    AIPolicy,
    BugBountyProfile,
    DatasetProfile,
    PayoffType,
    ProposedContract,
    Provenance,
    RiskDisclosure,
    RiskItem,
    RiskLevel,
    ScopeClause,
    Terms,
)

_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


def _risk(level=RiskLevel.LOW):
    return RiskItem(level=level, justification="grounded in program scope")


def make_bounty(**over):
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


def make_dataset(**over):
    """A valid dataset-lane contract (portfolio, licensed, no PII)."""
    kw = dict(
        contract_id="ct-ds-1",
        proposer_seat="bob.proposer.glm",
        proposed_at=_NOW,
        scope=ScopeClause(
            targets=["kaggle:acme-corpus"],
            in_scope_actions=["ingest source", "verify rows"],
            out_of_scope=["scrape non-listed sources"],
            authorization_basis="dataset license + resale rights",
            ai_policy=AIPolicy.ALLOWED,
        ),
        terms=Terms(
            burn_budget_usd=4.0, payoff_type=PayoffType.PORTFOLIO,
            expected_payoff_usd=None, payout_timeline="portfolio", deadline=None,
        ),
        risk=RiskDisclosure(
            authorization_certainty=_risk(), duplicate_likelihood=_risk(),
            reputational_exposure=_risk(), claim_confidence=0.7,
            claim_confidence_basis="licenses verified", known_unknowns="none identified",
        ),
        provenance=Provenance(
            source_urls=["https://kaggle.com/acme-corpus"],
            terms_snapshot_sha256="a" * 64, fetched_at=_NOW,
        ),
        profile=DatasetProfile(
            dataset_spec="acme corpus v1", source_licenses=["CC-BY-4.0"],
            pii_present=False, resale_rights_basis="license permits redistribution",
            verification_method="per-row checksum",
        ),
    )
    kw.update(over)
    return ProposedContract(**kw)


@pytest.fixture
def bounty():
    """Factory: call ``bounty(**overrides)`` to build a valid bug-bounty contract."""
    return make_bounty


@pytest.fixture
def dataset():
    """Factory: call ``dataset(**overrides)`` to build a valid dataset-lane contract."""
    return make_dataset
