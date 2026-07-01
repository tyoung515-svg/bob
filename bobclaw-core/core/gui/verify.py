from __future__ import annotations

"""Deterministic (Default-FAIL) verification of subgoal post-conditions (§2.6).

Every postcondition criterion starts false and flips true only on positive
evidence from the post-action frame and the frame-diff between pre- and post-action
states.  The module is PURE: no I/O, no mutable global state, no randomness.
"""

from core.gui.framediff import a11y_contains
from core.gui.types import Frame, FrameDiff, Postcondition, Verdict


def verify_postcondition(
    pc: Postcondition,
    prev: Frame | None,  # unused in this version, kept for future extensions
    post: Frame,
    diff: FrameDiff,
) -> Verdict:
    """Check that every declared criterion in *pc* holds against the post-action frame.

    Criterion list (in evaluation order):
    * ``changed`` — iff *pc.expect_changed* is True and *diff.changed* is True.
    * ``present:<k>`` — the node identified by *k* (node_id or name) exists in *post*.
    * ``absent:<k>`` — the node identified by *k* does NOT exist in *post*.
    * ``text_in:<k>`` — the value of the node identified by *k* contains *sub*.

    Returns a ``Verdict`` with ``ok=True`` iff ALL criteria pass.
    If the criteria list is empty (no criteria were defined), ``ok`` is False
    with reason ``"no postcondition criteria"``.
    """
    criteria: list[tuple[str, bool]] = []

    if pc.expect_changed:
        criteria.append(("changed", diff.changed))

    # An empty key is unverifiable → the criterion fails CLOSED (Default-FAIL), so a
    # degenerate present("")/absent("")/text_in("") can never trivially pass.
    for k in pc.present:
        passed = bool(k) and (a11y_contains(post, node_id=k) or a11y_contains(post, name=k))
        criteria.append((f"present:{k}", passed))

    for k in pc.absent:
        # True only if the (non-empty) node is genuinely absent.
        passed = bool(k) and not (a11y_contains(post, node_id=k) or a11y_contains(post, name=k))
        criteria.append((f"absent:{k}", passed))

    for k, sub in pc.text_in:
        passed = bool(k) and (
            a11y_contains(post, node_id=k, value_substr=sub)
            or a11y_contains(post, name=k, value_substr=sub)
        )
        criteria.append((f"text_in:{k}", passed))

    if not criteria:
        return Verdict(ok=False, reason="no postcondition criteria", criteria=())

    ok = all(v for _, v in criteria)
    reason = ""
    if not ok:
        # first failing criterion's name
        for name, value in criteria:
            if not value:
                reason = f"failed: {name}"
                break
    return Verdict(ok=ok, reason=reason, criteria=tuple(criteria))
