"""E1 earning layer — grant → Gate Scope adapter (MS#4 · Slice 0).

Bridges a ratified contract's ForestOS grant (``ProposedContract.to_grant()``) to the Gate
Router's :class:`core.permissions.Scope`, so the SAME deterministic policy
(:func:`core.permissions.evaluate_action`) governs an earning contract's execution — no model
in the path (the E1 no-AI-in-any-gate invariant). PURE.

Slice 0 maps the scope triple (targets / in-scope / out-of-scope). Spend-metering
(``burn_budget_usd`` → a live cap) is DEP-2, a later slice.
"""
from __future__ import annotations

from core.permissions import Scope


def grant_to_scope(grant: dict) -> Scope:
    """Map a contract grant (:meth:`ProposedContract.to_grant`) to a Gate :class:`Scope`.

    ``in_scope_actions`` → ``auto_actions`` (pre-approved); ``targets`` → ``may_touch``;
    ``out_of_scope`` → ``may_not_touch`` (the hard boundary, I6). An action neither in scope
    nor an always-human floor fails closed to the critic in ``evaluate_action`` (never auto).
    """
    scope = grant.get("scope") or {}
    return Scope(
        may_touch=list(scope.get("targets") or []),
        may_not_touch=list(scope.get("deny") or []),
        auto_actions=list(scope.get("allow") or []),
        escalate_actions=[],
    )


__all__ = ["grant_to_scope"]
