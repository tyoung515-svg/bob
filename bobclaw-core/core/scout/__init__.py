"""BoBClaw Scout module (M1) — sources v0 + dossier format (MS8-SC1).

Additive-new package. Public surface:
  * ``dossier`` — the evidence-bundle schema (claims + evidence refs + EST cost-impact
    + maturity tags), serialize<->load round-trip, fail-loud validation.
  * ``sources`` — Scout's sources registry v0, riding the Watch-pipe substrate (S1) by
    read-only reuse of ``core.watch.registry`` (no schema fork).

Scout PROPOSES, never applies (MODULES.md D1): this slice ships only the data schema +
the fuller sources list. The absorb proposal artifact is SC2.
"""
from __future__ import annotations

from core.scout.dossier import (
    Category,
    Claim,
    CostDirection,
    CostImpact,
    Dossier,
    EvidenceRef,
    Maturity,
    SCHEMA_VERSION,
    Verification,
    VerificationStatus,
    dump_dossier,
    load_dossier,
)
from core.scout.sources import (
    load_scout_sources,
    scout_sources_path,
)
from core.scout.proposal import (
    AbsorbCandidate,
    AbsorbProposal,
    ManifestDiff,
    build_proposal,
    load_proposal,
    write_proposal,
)
from core.scout.stack_audit import (
    Money,
    Recommendation,
    StackAuditReport,
    Subscription,
    SubscriptionsDoc,
    UsageTelemetry,
    audit_stack,
    dump_report,
    load_subscriptions,
    load_usage_telemetry,
)

__all__ = [
    "Category",
    "Claim",
    "CostDirection",
    "CostImpact",
    "Dossier",
    "EvidenceRef",
    "Maturity",
    "SCHEMA_VERSION",
    "Verification",
    "VerificationStatus",
    "dump_dossier",
    "load_dossier",
    "load_scout_sources",
    "scout_sources_path",
    "AbsorbCandidate",
    "AbsorbProposal",
    "ManifestDiff",
    "build_proposal",
    "load_proposal",
    "write_proposal",
    "Money",
    "Recommendation",
    "StackAuditReport",
    "Subscription",
    "SubscriptionsDoc",
    "UsageTelemetry",
    "audit_stack",
    "dump_report",
    "load_subscriptions",
    "load_usage_telemetry",
]
