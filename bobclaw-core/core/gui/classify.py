"""Deterministic classification of step failures (PURE).

The deterministic floor of the unified §4 Recovery taxonomy. The model-driven failure
*adjudication* is step-6; here the failure type is a fixed precedence over the action,
the frame-diff, the verdict, and the post-frame's a11y roles.
"""
from __future__ import annotations

from core.gui.types import Action, FailureType, Frame, FrameDiff, Verdict

# a11y role keyword groups (case-insensitive substring match). Category precedence is
# modal > auth > loading, applied ACROSS ALL nodes (not node-by-node) so the result is
# independent of a11y node ordering.
_MODAL_KEYWORDS: tuple[str, ...] = ("dialog", "modal", "alert")
_AUTH_KEYWORDS: tuple[str, ...] = ("login", "signin", "auth", "password")
_LOADING_KEYWORDS: tuple[str, ...] = ("progress", "spinner", "loading", "busy")


def _any_role_matches(post: Frame, keywords: tuple[str, ...]) -> bool:
    """True iff any post-frame a11y node's role contains any keyword (case-insensitive)."""
    return any(kw in node.role.lower() for node in post.a11y for kw in keywords)


def classify_failure(
    action: Action | None,
    diff: FrameDiff | None,
    verdict: Verdict | None,
    post: Frame,
) -> FailureType:
    """Classify one step's failure in fixed precedence.

    1. ``action is None`` → PARSE_ERROR.
    2. verdict passed → NONE.
    3. post a11y shows a modal/auth/loading role (category precedence) → that block type.
    4. nothing changed → NO_STATE_CHANGE (the silent-input / dead-click signature).
    5. something changed but the post-condition failed → WRONG_ELEMENT.
    6. otherwise → IMPOSSIBLE.
    """
    if action is None:
        return FailureType.PARSE_ERROR
    if verdict is not None and verdict.ok:
        return FailureType.NONE
    if _any_role_matches(post, _MODAL_KEYWORDS):
        return FailureType.MODAL_INTERRUPT
    if _any_role_matches(post, _AUTH_KEYWORDS):
        return FailureType.AUTH_BLOCK
    if _any_role_matches(post, _LOADING_KEYWORDS):
        return FailureType.LOADING
    if diff is not None and not diff.changed:
        return FailureType.NO_STATE_CHANGE
    if diff is not None and diff.changed:
        return FailureType.WRONG_ELEMENT
    return FailureType.IMPOSSIBLE
