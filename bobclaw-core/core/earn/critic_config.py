"""E1 — critic seat-pinning + model-family divergence (MS#4 · Slice 1, D-1 / FM-3).

The critic invariant is NOT a hollow ``critic_seat != proposer_seat`` identity check — that is
correlated-failure-blind if both seats share a model family (exactly what a critic must catch).
So the resolver enforces a seat-config invariant: ``critic_family ⊥ proposer_family``, with the
critic seat FIXED PER LANE (a stronger reasoning seat for smart-contract logic; a cheaper seat
for dataset license checks). Reuses :func:`core.verify.postcondition.family_of` — never
re-implements the family map. PURE.
"""
from __future__ import annotations

from typing import Optional

from core.earn.contract import Lane
from core.verify.postcondition import family_of


class CriticConfigError(ValueError):
    """The critic seat shares a model family with the proposer (correlated-failure-blind)."""


# Lane-pinned critic backends (D-1): consistency of critic caliber WITHIN a lane beats
# per-contract optimization. Concrete backends (their family is derived via family_of).
LANE_CRITIC_BACKEND: dict[Lane, str] = {
    Lane.BUG_BOUNTY: "minimax",   # senior reasoning tier for smart-contract logic
    Lane.DATASET: "glm_5_2",      # cheaper for license / PII checks
}


def critic_backend_for(lane: Lane) -> str:
    """The lane-pinned critic backend (D-1)."""
    return LANE_CRITIC_BACKEND.get(lane, "minimax")


def assert_family_divergence(proposer_backend: str, critic_backend: str) -> None:
    """FM-3 / D-1: raise :class:`CriticConfigError` iff critic and proposer share a family."""
    if family_of(proposer_backend) == family_of(critic_backend):
        raise CriticConfigError(
            f"critic family {family_of(critic_backend)!r} == proposer family "
            f"{family_of(proposer_backend)!r} — a same-family critic is correlated-failure-blind"
        )


def resolve_critic_backend(lane: Lane, proposer_backend: str) -> str:
    """The lane critic, asserting family divergence from the proposer (raises on collision)."""
    critic = critic_backend_for(lane)
    assert_family_divergence(proposer_backend, critic)
    return critic


def resolve_lane_seats(
    lane: Lane,
    *,
    proposer_backend: Optional[str] = None,
    team: Optional[str] = None,
) -> tuple[str, str]:
    """JOAT seat wiring (D-1): resolve (proposer_backend, critic_backend) for an earning lane.

    The proposer comes from the active JOAT team's ``worker`` role (:func:`core.teams.role_backend`)
    when a ``team`` is given, else the explicit ``proposer_backend``. The critic is the
    lane-pinned seat, with family divergence asserted against the proposer (raises on collision).
    """
    from core.teams import role_backend

    proposer = (role_backend(team, "worker") if team else None) or proposer_backend
    if not proposer:
        raise CriticConfigError("no proposer backend (give proposer_backend or a team with a worker role)")
    critic = resolve_critic_backend(lane, proposer)
    return proposer, critic


__all__ = [
    "CriticConfigError", "LANE_CRITIC_BACKEND", "critic_backend_for",
    "assert_family_divergence", "resolve_critic_backend", "resolve_lane_seats",
]
