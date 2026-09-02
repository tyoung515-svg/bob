"""E1 — deterministic audit hook (MS#4 · Slice 2, D-3). No model in the path.

Each executed action is compared by SET MEMBERSHIP against the FROZEN ratified envelope
(the scope snapshotted at ``ratified_envelope_sha256``). The model is NEVER in the audit path.

- Bounty: action ∉ ratified ``in_scope_actions``, OR target ∉ ratified ``targets``, OR target ∈
  ``out_of_scope`` ⇒ unauthorized ⇒ hard-deny.
- Dataset: source ∉ ratified ``targets``, OR a source without a ratified license, OR a PII field
  ⇒ violation.

A hard-deny is where the caller writes the ``approved_by='gate'`` ledger row (the Gate-Router
audit trail). PURE.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from core.earn.contract import BugBountyProfile, DatasetProfile, ProposedContract


@dataclass(frozen=True)
class AuditEvent:
    contract_id: str
    action: str
    target: str
    action_hash: str
    scope_snapshot_ref: Optional[str]   # the ratified_envelope_sha256 this was audited against


@dataclass(frozen=True)
class AuditVerdict:
    authorized: bool
    reason: str
    event: AuditEvent


def _hash_action(action: str, target: str) -> str:
    blob = json.dumps([action, target], separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def audit_action(
    contract: ProposedContract,
    action: str,
    target: str,
    *,
    source_license: Optional[str] = None,
    is_pii: bool = False,
) -> AuditVerdict:
    """Deterministic set-membership audit of one action against the ratified envelope.

    Raises ``ValueError`` if the contract is not ratified (no frozen envelope to audit against).
    """
    if contract.ratified_scope is None:
        raise ValueError("contract not ratified; no envelope to audit against")
    env = contract.ratified_scope
    event = AuditEvent(
        contract_id=contract.contract_id, action=action, target=target,
        action_hash=_hash_action(action, target),
        scope_snapshot_ref=contract.ratified_envelope_sha256,
    )

    def deny(reason: str) -> AuditVerdict:
        return AuditVerdict(False, reason, event)

    # Target boundary — both lanes: an explicit out-of-scope target, or a target outside the
    # ratified set, is unauthorized.
    if target in (env.out_of_scope or []):
        return deny(f"target {target!r} is explicitly out_of_scope")
    if target not in (env.targets or []):
        return deny(f"target {target!r} not in ratified targets")

    if isinstance(contract.profile, BugBountyProfile):
        if action not in (env.in_scope_actions or []):
            return deny(f"action {action!r} not in ratified in_scope_actions")
    elif isinstance(contract.profile, DatasetProfile):
        if is_pii:
            return deny("PII field present in dataset source")
        if not source_license or source_license not in (contract.profile.source_licenses or []):
            return deny(f"source license {source_license!r} not ratified")

    return AuditVerdict(True, "authorized", event)


__all__ = ["AuditEvent", "AuditVerdict", "audit_action"]
