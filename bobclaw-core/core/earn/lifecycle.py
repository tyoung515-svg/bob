"""E1 — lifecycle: decay/cleanup sweep + the operator surface (MS#4 · Slice 3).

Deterministic, no model. ``sweep_decayed`` closes ratified contracts whose deadline has passed
(the ``decays_at`` of the ForestOS grant); the :class:`OperatorSurface` is the thin ratify /
modify / reject adapter — the ONLY face on the earning layer, and every write is an operator
action (the human is the sole ratifier, I4). PURE (the clock is injected).
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from core.earn.contract import ContractStatus, ProposedContract, ScopeClause, Terms

_TERMINAL = (ContractStatus.REJECTED, ContractStatus.AUTO_REJECTED, ContractStatus.CLOSED)


def deadline_of(contract: ProposedContract) -> Optional[datetime]:
    """The binding deadline: the ratified terms' if ratified, else the proposal's."""
    terms = contract.ratified_terms or contract.terms
    return terms.deadline if terms else None


def is_decayed(contract: ProposedContract, now: datetime) -> bool:
    dl = deadline_of(contract)
    return dl is not None and now > dl


def sweep_decayed(contracts: Iterable[ProposedContract], *, now: datetime) -> list[str]:
    """Close every non-terminal contract past its deadline. Returns the swept contract_ids.
    Deterministic + idempotent (a CLOSED/rejected contract is skipped)."""
    swept: list[str] = []
    for c in contracts:
        if c.status in _TERMINAL:
            continue
        if is_decayed(c, now):
            c.status = ContractStatus.CLOSED
            c.modifications = list(c.modifications) + [f"DECAYED/closed at {now.isoformat()}"]
            swept.append(c.contract_id)
    return swept


class OperatorSurface:
    """The operator-facing ratify / modify / reject adapter (no model; an operator write)."""

    def __init__(self, contract: ProposedContract):
        self.contract = contract

    def ratify(self, operator: str) -> str:
        """Accept as-proposed; freeze + hash the envelope. Returns the envelope sha256."""
        return self.contract.ratify(ratifier=operator)

    def modify(
        self,
        operator: str,
        *,
        scope: Optional[ScopeClause] = None,
        terms: Optional[Terms] = None,
        modifications: Optional[list[str]] = None,
    ) -> str:
        """Accept WITH binding edits to scope/terms (I4 — the operator's version is the envelope)."""
        return self.contract.ratify(
            ratifier=operator, scope=scope, terms=terms,
            modifications=modifications or ["operator modification"],
        )

    def reject(self, operator: str, reason: str) -> None:
        self.contract.reject(ratifier=operator, reason=reason)


__all__ = ["deadline_of", "is_decayed", "sweep_decayed", "OperatorSurface"]
