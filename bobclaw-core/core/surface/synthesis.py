"""P1 notebook-grounded surface — ForestOS synthesis grant + reactive approval (MS#4 · Slice 6).

Synthesis (querying ACROSS notebooks) is not ambient: you cannot accidentally roam because you
don't hold the roaming handle. A :class:`SynthesisRetrieval` is minted ONLY from a
:class:`SynthesisGrant`, which is created ONLY on explicit approval, scoped to a defined notebook
set, and DECAYS (default 1h). D-3 reactive approval: the request is produced only when the
router/escalation says a query hit the edge ("this also touches Notebook B — allow?"), legible
and reactive, never a pre-selected mode. PURE (clock injected).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core.surface.escalation import EscalationRequest, EscalationTier


class SynthesisError(RuntimeError):
    """Synthesis was attempted without a valid, in-scope, un-expired grant."""


@dataclass(frozen=True)
class SynthesisApprovalRequest:
    """The reactive, legible approval prompt surfaced to the human (D-3)."""
    notebook_ids: tuple[str, ...]
    tier: str
    reason: str


@dataclass(frozen=True)
class SynthesisGrant:
    grant_id: str
    user: str
    notebook_ids: frozenset
    issued_at: float
    decays_at: float
    reason: str

    def is_valid(self, now: float) -> bool:
        return now < self.decays_at

    def covers(self, notebook_id: str) -> bool:
        return notebook_id in self.notebook_ids


def request_synthesis(esc: EscalationRequest) -> Optional[SynthesisApprovalRequest]:
    """D-3 reactive trigger: only a NON-grounded escalation produces an approval request; a
    grounded decision needs no synthesis (answer from the one notebook). Returns None if grounded."""
    if esc.tier is EscalationTier.GROUNDED:
        return None
    return SynthesisApprovalRequest(tuple(esc.notebook_ids), esc.tier.value, esc.reason)


def create_synthesis_grant(
    request: SynthesisApprovalRequest,
    user: str,
    *,
    now: float,
    decay_seconds: float = 3600.0,
    approved: bool = False,
) -> SynthesisGrant:
    """Mint a scoped, decaying grant — ONLY on explicit approval (no ambient synthesis, §2)."""
    if not approved:
        raise SynthesisError("synthesis grant requires explicit approval (no ambient synthesis)")
    ids = tuple(request.notebook_ids)
    if not ids:
        raise SynthesisError("synthesis grant needs a non-empty notebook scope")
    return SynthesisGrant(
        grant_id=f"synth::{user}::{'-'.join(sorted(ids))}",
        user=user,
        notebook_ids=frozenset(ids),
        issued_at=now,
        decays_at=now + decay_seconds,
        reason=request.reason,
    )


class SynthesisRetrieval:
    """A handle over a DEFINED notebook set — minted only from a valid grant; no ambient roam."""

    def __init__(self, grant: SynthesisGrant, providers: dict, *, clock: Callable[[], float]):
        if not grant.is_valid(clock()):
            raise SynthesisError("synthesis grant is expired")
        for nb in providers:
            if not grant.covers(nb):
                raise SynthesisError(f"provider for {nb!r} is outside the grant scope")
        self._grant = grant
        self._providers = dict(providers)
        self._clock = clock

    @property
    def scope(self) -> frozenset:
        return self._grant.notebook_ids

    async def vector_search(self, notebook_id: str, query_vector, *, k: int = 10):
        if not self._grant.is_valid(self._clock()):
            raise SynthesisError("synthesis grant expired mid-session")
        if not self._grant.covers(notebook_id):
            raise SynthesisError(
                f"{notebook_id!r} not in grant scope {sorted(self._grant.notebook_ids)}"
            )
        prov = self._providers.get(notebook_id)
        if prov is None:
            raise SynthesisError(f"no bound provider for {notebook_id!r}")
        return await prov.vector_search(query_vector, k=k)


__all__ = [
    "SynthesisError", "SynthesisApprovalRequest", "SynthesisGrant",
    "request_synthesis", "create_synthesis_grant", "SynthesisRetrieval",
]
