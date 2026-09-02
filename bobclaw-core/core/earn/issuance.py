"""E1 — grant issuance with the FM-2 TOCTOU re-check (MS#4 · Slice 2, I3).

The frozen ratified hash must not freeze trust in the WORLD: between ratification and executor-
token issuance the world moves (scope narrows, a license is revoked, terms change). So at
ISSUANCE (not audit — too late) we re-fetch ``source_urls`` via the mechanical fetcher (FM-4),
recompute the snapshot hash, and HARD-DENY if it != the stored ``terms_snapshot_sha256``; and we
check ``decays_at`` (the deadline) against the wall clock.

The fetcher is INJECTED (FM-4 is a later DEP). Absent ⇒ provenance is UNVERIFIED ⇒ refuse to
issue (needs_review), never issue on the proposer's self-attested snapshot (I8). PURE — clock
and fetcher are both injected.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from core.earn.contract import ProposedContract

# A fetcher recomputes the snapshot sha256 over the CURRENT fetched terms for the given urls.
Fetcher = Callable[[list[str]], str]


class IssuanceError(ValueError):
    """Issuance refused: not ratified, decayed, TOCTOU snapshot mismatch, or unverifiable."""


def issue_grant(
    contract: ProposedContract,
    *,
    now: datetime,
    fetcher: Optional[Fetcher] = None,
) -> dict:
    """Emit the executor grant IFF the contract is ratified, un-decayed, and its terms snapshot
    still verifies against a fresh fetch. Raises :class:`IssuanceError` otherwise."""
    if contract.ratified_scope is None or contract.ratified_terms is None:
        raise IssuanceError("contract not ratified; nothing to issue")
    deadline = contract.ratified_terms.deadline
    if deadline is not None and now > deadline:
        raise IssuanceError(f"contract decayed: deadline {deadline.isoformat()} < issuance time")
    # FM-2 / I8: unverified provenance is not issuable — no fetcher = no re-verification.
    if fetcher is None:
        raise IssuanceError(
            "provenance unverified: no fetcher to re-check terms at issuance (needs_review)"
        )
    current = fetcher(contract.provenance.source_urls)
    if current != contract.provenance.terms_snapshot_sha256:
        raise IssuanceError("TOCTOU: terms snapshot changed since ratification (hard-deny)")
    return contract.to_grant()


__all__ = ["Fetcher", "IssuanceError", "issue_grant"]
