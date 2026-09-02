"""
proposed_contract.py

Bob's proposed-contract envelope: the origination-side object where Bob proposes
paid work as a scoped, ratifiable grant, and the human partner accepts / modifies
/ rejects the terms.

Lifecycle (propose -> ratify -> execute -> audit-against-ratified):

  1. Bob (proposer seat) emits a ProposedContract: opportunity + proposed scope.
  2. A critic seat, DISTINCT from the proposer, attaches a CriticVerdict.
  3. run_triage() sorts it into auto_reject / fast_approve / needs_review so a
     day's worth of contracts collapses to a short human queue.
  4. The human ratify()s, optionally MODIFYING the scope/terms. The *ratified*
     scope - not the proposed one - is frozen into a ForestOS grant and hashed.
     That hash is the reference the git-tree audit enforces execution against.

Design invariant: nothing downstream trusts the proposal's self-reported honesty.
A proposal that low-balls its own risk still cannot authorize overreach, because
(a) the critic is a separate seat, (b) risk fields are mandatory and each rating
must carry a justification, and (c) the post-execution audit checks against the
ratified envelope hash regardless of what the proposal claimed.

Pydantic v2.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class Lane(str, Enum):
    BUG_BOUNTY = "bug_bounty"
    DATASET = "dataset"


class PayoffType(str, Enum):
    PORTFOLIO = "portfolio"   # reputation / evidence only, no cash expected
    CASH = "cash"
    MIXED = "mixed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AIPolicy(str, Enum):
    WELCOMED = "welcomed"       # program explicitly invites AI-assisted work
    ALLOWED = "allowed"
    UNSPECIFIED = "unspecified"
    BANNED = "banned"           # hard auto-reject: submitting here burns the identity


class CriticStance(str, Enum):
    PASS = "pass"
    PASS_WITH_CONCERNS = "pass_with_concerns"
    BLOCK = "block"


class ContractStatus(str, Enum):
    PROPOSED = "proposed"
    CRITIQUED = "critiqued"
    AUTO_REJECTED = "auto_rejected"
    PENDING_REVIEW = "pending_review"
    RATIFIED = "ratified"
    MODIFIED_RATIFIED = "modified_ratified"
    REJECTED = "rejected"
    EXECUTING = "executing"
    AUDITED = "audited"
    CLOSED = "closed"


class TriageBucket(str, Enum):
    AUTO_REJECT = "auto_reject"     # policy-killed, logged, you only spot-audit these
    FAST_APPROVE = "fast_approve"   # clean, low-stakes, portfolio-only
    NEEDS_REVIEW = "needs_review"   # your review budget goes here


# --------------------------------------------------------------------------- #
# Shared sub-objects
# --------------------------------------------------------------------------- #

class RiskItem(BaseModel):
    """A rated risk. A bare level with no justification is invalid on purpose:
    it raises the cost of low-balling a risk to get a contract approved."""
    level: RiskLevel
    justification: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class RiskDisclosure(BaseModel):
    """All fields mandatory so an omission is a validation error, not a silent gap."""
    authorization_certainty: RiskItem   # how sure are we we're allowed to do this at all
    duplicate_likelihood: RiskItem      # bounty lane: has this probably been reported already
    reputational_exposure: RiskItem     # what a bad submission costs the researcher identity
    claim_confidence: float = Field(ge=0.0, le=1.0)   # confidence in the core payoff claim
    claim_confidence_basis: str = Field(min_length=1)
    known_unknowns: str = Field(min_length=1)         # must be explicit, e.g. "none identified"


class ScopeClause(BaseModel):
    """The grant core. out_of_scope is the HARD boundary the audit enforces."""
    targets: list[str]                  # assets / programs / datasets in play
    in_scope_actions: list[str]         # exactly what Bob is scoped to do
    out_of_scope: list[str]             # hard boundaries; crossing them trips the audit
    authorization_basis: str            # why we are permitted (program terms, license, ...)
    ai_policy: AIPolicy = AIPolicy.UNSPECIFIED


class Terms(BaseModel):
    burn_budget_usd: float = Field(ge=0.0)   # Bob's own compute/token spend cap to chase this
    payoff_type: PayoffType
    expected_payoff_usd: Optional[float] = None   # None/0 when portfolio-only
    payout_timeline: str                          # e.g. "on-accept, ~30-90d triage backlog"
    deadline: Optional[datetime] = None


class Provenance(BaseModel):
    source_urls: list[str]
    terms_snapshot_sha256: str          # hash of the source terms as fetched at propose-time
    fetched_at: datetime


class CriticVerdict(BaseModel):
    critic_seat: str                    # must differ from proposer_seat (validated on parent)
    stance: CriticStance
    concerns: list[str] = Field(default_factory=list)
    dissent: Optional[str] = None
    reviewed_at: datetime


# --------------------------------------------------------------------------- #
# Lane profiles (two specializations of one envelope)
# --------------------------------------------------------------------------- #

class BugBountyProfile(BaseModel):
    lane: Literal[Lane.BUG_BOUNTY] = Lane.BUG_BOUNTY
    platform: str                       # hackerone / bugcrowd / immunefi / direct
    program_handle: str
    rules_of_engagement: str            # pulled verbatim from the program
    poc_required: bool = True           # Bob may not submit without a reproduced PoC
    novelty_check_done: bool = False    # dupe search run before submission
    severity_claimed: Optional[str] = None   # CVSS / program scale, set at execution time


class DatasetProfile(BaseModel):
    lane: Literal[Lane.DATASET] = Lane.DATASET
    dataset_spec: str
    source_licenses: list[str]          # license governing each source
    pii_present: bool
    resale_rights_basis: str            # why we can legally sell the result
    verification_method: str            # how each record is verified (this is the product)
    row_target: Optional[int] = None


Profile = Union[BugBountyProfile, DatasetProfile]


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #

class ProposedContract(BaseModel):
    contract_id: str
    proposer_seat: str
    proposed_at: datetime

    scope: ScopeClause
    terms: Terms
    risk: RiskDisclosure
    provenance: Provenance
    profile: Profile = Field(discriminator="lane")

    critic: Optional[CriticVerdict] = None

    # populated by the pipeline
    status: ContractStatus = ContractStatus.PROPOSED
    triage_bucket: Optional[TriageBucket] = None
    auto_reject_reasons: list[str] = Field(default_factory=list)
    # D-2 (FM-1): set at critique by the PoC compound gate (core/earn/poc_gate.py). A bounty
    # with poc_reproduced != True can NEVER land in fast_approve (enforced in run_triage).
    poc_reproduced: Optional[bool] = None

    # populated by the human at ratification
    ratified_scope: Optional[ScopeClause] = None    # may be a MODIFIED copy of `scope`
    ratified_terms: Optional[Terms] = None
    ratifier: Optional[str] = None
    ratified_at: Optional[datetime] = None
    modifications: list[str] = Field(default_factory=list)
    ratified_envelope_sha256: Optional[str] = None

    # ----- invariants ----- #

    @model_validator(mode="after")
    def _critic_seat_distinct(self) -> "ProposedContract":
        if self.critic and self.critic.critic_seat == self.proposer_seat:
            raise ValueError("critic seat must differ from proposer seat")
        return self

    # ----- audit binding ----- #

    @staticmethod
    def canonical_digest(scope: ScopeClause, terms: Terms) -> str:
        """Stable hash over the ratified scope+terms. The git-tree audit checks
        execution against THIS, not against the proposal."""
        payload = json.dumps(
            {"scope": scope.model_dump(mode="json"),
             "terms": terms.model_dump(mode="json")},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def audit_reference(self) -> str:
        if self.ratified_envelope_sha256 is None:
            raise ValueError("contract not ratified; no audit reference exists yet")
        return self.ratified_envelope_sha256

    # ----- triage ----- #

    def run_triage(self) -> TriageBucket:
        """Sort into auto_reject / fast_approve / needs_review. Keeps the human
        queue short: you spend review budget on NEEDS_REVIEW and spot-audit the
        AUTO_REJECT log."""
        reasons: list[str] = []

        # hard auto-rejects
        if isinstance(self.profile, BugBountyProfile) and self.scope.ai_policy == AIPolicy.BANNED:
            reasons.append("program prohibits AI-assisted submissions")
        if not self.scope.in_scope_actions:
            reasons.append("no in-scope actions (out-of-scope-only)")
        if self.terms.payoff_type is not PayoffType.PORTFOLIO:
            payoff = self.terms.expected_payoff_usd
            if payoff is not None and payoff <= self.terms.burn_budget_usd:
                reasons.append("non-positive ROI (expected payoff <= burn budget)")
        if self.risk.duplicate_likelihood.level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            reasons.append("high duplicate likelihood")
        if self.critic and self.critic.stance is CriticStance.BLOCK:
            reasons.append(f"critic blocked: {self.critic.dissent or 'see concerns'}")

        if reasons:
            self.auto_reject_reasons = reasons
            self.triage_bucket = TriageBucket.AUTO_REJECT
            self.status = ContractStatus.AUTO_REJECTED
            return self.triage_bucket

        # needs-your-eyes predicates
        needs_review = (
            self.terms.payoff_type in (PayoffType.CASH, PayoffType.MIXED)
            or self.risk.authorization_certainty.level in (
                RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
            or self.risk.reputational_exposure.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            or (self.critic is not None and self.critic.stance is CriticStance.PASS_WITH_CONCERNS)
            # D-2: a bounty without a reproduced PoC can never fast-approve (I5 visible in triage).
            or (isinstance(self.profile, BugBountyProfile) and self.poc_reproduced is not True)
        )
        self.triage_bucket = (
            TriageBucket.NEEDS_REVIEW if needs_review else TriageBucket.FAST_APPROVE
        )
        self.status = ContractStatus.PENDING_REVIEW
        return self.triage_bucket

    # ----- ratification (the human write) ----- #

    def ratify(
        self,
        ratifier: str,
        scope: Optional[ScopeClause] = None,
        terms: Optional[Terms] = None,
        modifications: Optional[list[str]] = None,
    ) -> str:
        """Accept, optionally modifying scope/terms. Whatever is passed here (or
        the proposal's own scope/terms if nothing is) becomes the frozen, hashed,
        audit-enforced envelope. Your edits are binding, not advisory."""
        final_scope = scope or self.scope
        final_terms = terms or self.terms
        self.ratified_scope = final_scope
        self.ratified_terms = final_terms
        self.ratifier = ratifier
        self.ratified_at = datetime.now(timezone.utc)
        self.modifications = modifications or []
        self.ratified_envelope_sha256 = self.canonical_digest(final_scope, final_terms)
        self.status = (
            ContractStatus.MODIFIED_RATIFIED if (scope or terms)
            else ContractStatus.RATIFIED
        )
        return self.ratified_envelope_sha256

    def reject(self, ratifier: str, reason: str) -> None:
        self.ratifier = ratifier
        self.ratified_at = datetime.now(timezone.utc)
        self.modifications = [f"REJECTED: {reason}"]
        self.status = ContractStatus.REJECTED

    # ----- ForestOS grant emission ----- #

    def to_grant(self) -> dict:
        """Emit the ForestOS grant Bob executes under. Scoped, decaying, audited -
        the contract IS a grant request; this is its issued form."""
        if self.ratified_scope is None or self.ratified_terms is None:
            raise ValueError("contract not ratified; no grant to emit")
        return {
            "grant_id": f"grant::{self.contract_id}",
            "subject": self.proposer_seat,          # the seat authorized to execute
            "scope": {
                "targets": self.ratified_scope.targets,
                "allow": self.ratified_scope.in_scope_actions,
                "deny": self.ratified_scope.out_of_scope,   # hard boundary
            },
            "authorization_basis": self.ratified_scope.authorization_basis,
            "decays_at": (
                self.ratified_terms.deadline.isoformat()
                if self.ratified_terms.deadline else None
            ),
            "audit_reference": self.ratified_envelope_sha256,  # audit checks execution vs THIS
            "issued_by": self.ratifier,
            "issued_at": self.ratified_at.isoformat() if self.ratified_at else None,
        }


# --------------------------------------------------------------------------- #
# Worked examples
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    now = datetime.now(timezone.utc)

    # --- Example A: smart-contract bounty (cash) -> NEEDS_REVIEW --------------
    bounty = ProposedContract(
        contract_id="ct-2026-0001",
        proposer_seat="bob.proposer.glm",
        proposed_at=now,
        scope=ScopeClause(
            targets=["immunefi:acme-vault-v3 (github: acme/vault @ commit a1b2c3)"],
            in_scope_actions=[
                "static review of in-scope contracts",
                "local fork-test PoC of any candidate finding",
            ],
            out_of_scope=[
                "mainnet interaction",
                "testing front-end / off-chain infra",
                "any contract not listed in program scope",
            ],
            authorization_basis="Immunefi program scope, fetched terms snapshot",
            ai_policy=AIPolicy.ALLOWED,
        ),
        terms=Terms(
            burn_budget_usd=8.0,
            payoff_type=PayoffType.CASH,
            expected_payoff_usd=5000.0,
            payout_timeline="on-accept, on-chain settlement ~days",
            deadline=None,
        ),
        risk=RiskDisclosure(
            authorization_certainty=RiskItem(
                level=RiskLevel.LOW,
                justification="contract addresses match published program scope",
                evidence_refs=["immunefi program page", "terms snapshot hash"],
            ),
            duplicate_likelihood=RiskItem(
                level=RiskLevel.MEDIUM,
                justification="popular protocol, some surface likely already reviewed",
            ),
            reputational_exposure=RiskItem(
                level=RiskLevel.LOW,
                justification="fork-test PoC required before any submission",
            ),
            claim_confidence=0.4,
            claim_confidence_basis="candidate reentrancy path, not yet PoC-confirmed",
            known_unknowns="whether the guard modifier fully mitigates; needs fork test",
        ),
        provenance=Provenance(
            source_urls=["https://immunefi.com/bounty/acme/"],
            terms_snapshot_sha256="0" * 64,
            fetched_at=now,
        ),
        profile=BugBountyProfile(
            platform="immunefi",
            program_handle="acme-vault-v3",
            rules_of_engagement="no mainnet interaction; PoC on fork only; ...",
            poc_required=True,
        ),
        critic=CriticVerdict(
            critic_seat="bob.critic.sonnet5",
            stance=CriticStance.PASS_WITH_CONCERNS,
            concerns=["confirm the guard modifier is not a false-positive blocker"],
            reviewed_at=now,
        ),
    )

    print("A bucket:", bounty.run_triage().value)   # needs_review (cash + critic concerns)

    # human tightens scope before accepting
    tightened = bounty.scope.model_copy(update={
        "out_of_scope": bounty.scope.out_of_scope + ["no gas-griefing class findings"],
    })
    digest = bounty.ratify(
        ratifier="travis",
        scope=tightened,
        modifications=["added gas-griefing to out-of-scope"],
    )
    print("A status:", bounty.status.value)
    print("A audit ref:", digest[:16], "...")
    print("A grant:", json.dumps(bounty.to_grant(), indent=2)[:400], "...\n")

    # --- Example B: dataset (portfolio) -> FAST_APPROVE -----------------------
    dataset = ProposedContract(
        contract_id="ct-2026-0002",
        proposer_seat="bob.proposer.kimi",
        proposed_at=now,
        scope=ScopeClause(
            targets=["public SEA regulatory filings corpus"],
            in_scope_actions=["collect from listed CC-BY sources", "verify + structure records"],
            out_of_scope=["any source without an explicit reuse license", "any PII fields"],
            authorization_basis="all sources CC-BY or public-domain per license audit",
        ),
        terms=Terms(
            burn_budget_usd=15.0,
            payoff_type=PayoffType.PORTFOLIO,
            expected_payoff_usd=None,
            payout_timeline="n/a (portfolio artifact)",
        ),
        risk=RiskDisclosure(
            authorization_certainty=RiskItem(
                level=RiskLevel.LOW,
                justification="license audit passed for every listed source",
            ),
            duplicate_likelihood=RiskItem(
                level=RiskLevel.LOW, justification="no equivalent structured corpus found",
            ),
            reputational_exposure=RiskItem(
                level=RiskLevel.LOW, justification="verification method is auditable per-row",
            ),
            claim_confidence=0.8,
            claim_confidence_basis="sources confirmed reachable and parseable in pilot",
            known_unknowns="none identified",
        ),
        provenance=Provenance(
            source_urls=["https://example.gov/filings"],
            terms_snapshot_sha256="1" * 64,
            fetched_at=now,
        ),
        profile=DatasetProfile(
            dataset_spec="normalized SEA filings, 2020-2025, per-field provenance",
            source_licenses=["CC-BY-4.0"],
            pii_present=False,
            resale_rights_basis="CC-BY permits redistribution with attribution",
            verification_method="each record cross-checked against source; entailment-gated",
            row_target=50000,
        ),
    )

    print("B bucket:", dataset.run_triage().value)   # fast_approve
    dataset.ratify(ratifier="travis")
    print("B status:", dataset.status.value)

    # --- Example C: no-AI program -> AUTO_REJECT (never reaches your queue) ---
    blocked = bounty.model_copy(deep=True)
    blocked.contract_id = "ct-2026-0003"
    blocked.status = ContractStatus.PROPOSED
    blocked.scope = blocked.scope.model_copy(update={"ai_policy": AIPolicy.BANNED})
    print("C bucket:", blocked.run_triage().value)
    print("C reasons:", blocked.auto_reject_reasons)
