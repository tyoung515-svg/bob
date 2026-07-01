"""
core/gui/intent.py — MS2-G2 Deterministic Intent<->Action Anti-Desync Gate

Pre-action gate that confirms the action's real target element (the a11y node under
the coordinate or matched by target string) equals the formalized intent's named element.
A mismatch on a mutating tier yields HARD_STOP, preventing the bypass described in
DESIGN-MS-D1 §2.1/§5 (the desync Tier-2 cannot catch because the irreversible action
fires before any post‑condition check). Purely deterministic, no model calls, no I/O,
latency ≤10 ms (DECISIONS-MS2 D1/OD3).

Composes only:
    core.gui.types (A11yNode, Action, ActionKind, Frame)
    core.gui.framediff (a11y_index)
    core.gui.tiers (Tier, classify_gui_action, classify_tool)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.gui.types import A11yNode, Action, ActionKind, Frame
from core.gui.framediff import a11y_index
from core.gui.tiers import Tier, classify_gui_action, classify_tool

import core.permissions as permissions  # only for the Scope type hint (used stringified)


class GateDecision(str, Enum):
    """Three‑way gate decision for the desync check.

    ALLOW     – the action is faithful (real target == intent target).
    WARN      – desync / unconfirmable on a read‑only action (surface, do not block).
    HARD_STOP – desync / unconfirmable on a mutating tier, or a targetless Full‑Access action.
    """

    ALLOW = "allow"
    WARN = "warn"
    HARD_STOP = "hard_stop"


@dataclass(frozen=True, slots=True)
class FormalizedIntent:
    """The model's declared intention for the next action.

    ``target`` is the claimed element (node_id preferred, else name).
    ``declared_tier`` and ``kind`` are advisory only; the gate uses the *real* tier.
    """

    target: str = ""
    declared_tier: Tier = Tier.READ_ONLY
    kind: ActionKind | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Result of the intent‑action match (deterministic, pre‑action)."""

    decision: GateDecision
    matched: bool
    confirmable: bool
    action_tier: Tier
    real_key: str
    intent_key: str
    reason: str

    @property
    def hard_stop(self) -> bool:
        """True iff the decision is HARD_STOP."""
        return self.decision is GateDecision.HARD_STOP

    @property
    def allowed(self) -> bool:
        """True iff the decision is ALLOW."""
        return self.decision is GateDecision.ALLOW


# ─── Public helpers ────────────────────────────────────────────────────────────


def node_key(node: A11yNode | None) -> str:
    """Unambiguous identity key for an a11y node.

    Mirror of ``framediff.a11y_index`` keying:
    - Uses ``node.node_id`` if truthy.
    - Otherwise ``"{role}:{name}"``.
    - Returns ``""`` for ``None``.
    """
    if node is None:
        return ""
    if node.node_id:
        return node.node_id
    return f"{node.role}:{node.name}"


def hit_test(frame: Frame, coord: tuple[int, int]) -> A11yNode | None:
    """Pure bounds hit‑test against the accessibility tree.

    The element whose half‑open bounds ``(x, y, w, h)`` contain ``coord``.
    Nested/overlapping: smallest area wins; on exact area tie, the last node in
    document order wins (iterate and replace when ``area <= best_area``).

    Returns ``None`` if no node contains the coordinate.
    """
    # Total: a malformed coord (None / wrong-length / non-iterable) can never hit anything.
    try:
        cx, cy = coord
    except (TypeError, ValueError):
        return None
    best_node: A11yNode | None = None
    best_area: float | None = None

    for node in frame.a11y:
        bounds = node.bounds
        if bounds is None:
            continue
        # Total: malformed bounds (wrong length) or non-numeric bounds/coord can never contain a
        # point — skip the node (fail safe), never raise. (audit r1: a propagating exception in a
        # SAFETY gate could bypass the check.)
        try:
            x, y, w, h = bounds
            if w <= 0 or h <= 0:
                continue
            inside = (x <= cx < x + w) and (y <= cy < y + h)
        except (TypeError, ValueError):
            continue
        if inside:
            area = w * h
            if best_node is None or area <= best_area:  # smallest wins; last wins on exact-area tie
                best_node = node
                best_area = area

    return best_node


def resolve_action_target(action: Action, frame: Frame) -> A11yNode | None:
    """Determine the a11y node that the action *really* targets.

    Priority:
    1. ``action.coord`` set → hit‑test (physical truth; coord wins over target string).
    2. ``action.target`` non‑empty → first node with matching ``node_id``, else first with matching ``name``.
    3. Otherwise → ``None`` (targetless).
    """
    if action.coord is not None:
        return hit_test(frame, action.coord)

    target = action.target
    if not target:
        return None

    # Fast path: try node_id via a11y_index (dictionary)
    idx = a11y_index(frame)
    if target in idx:
        return idx[target]

    # Fallback: first node whose name matches
    for node in frame.a11y:
        if node.name == target:
            return node
    return None


def resolve_intent_target(intent: FormalizedIntent, frame: Frame) -> A11yNode | None:
    """Find the a11y node named by the intent.

    If ``intent.target`` is empty → ``None`` (targetless intent).
    Otherwise: first node with matching ``node_id``, else first with matching ``name``.
    """
    target = intent.target
    if not target:
        return None

    idx = a11y_index(frame)
    if target in idx:
        return idx[target]

    for node in frame.a11y:
        if node.name == target:
            return node
    return None


def action_effective_tier(
    action: Action,
    real_node: A11yNode | None = None,
    scope: "permissions.Scope | None" = None,
) -> Tier:
    """Compose the **real** effect tier of an action (composes G1; does NOT duplicate it).

    Base = ``classify_gui_action(action)`` (the trustworthy by‑kind floor: NOOP/SCROLL→READ_ONLY,
    KEY/TYPE/CLICK→WRITE_LOCAL).

    The element‑identity signals — ``action.target`` and the real hit element's ``name``/``role`` —
    are DESCRIPTIVE labels, not tool names. So they may only **escalate** the tier on a *recognised
    dangerous/outward* classification (``>= SOCIAL``): a click whose real element is a "Delete account"
    button → ``classify_tool`` returns ``FULL_ACCESS`` → escalate. A *benign* label ("OK", "Results")
    classifies as ``classify_tool``'s ``WRITE_LOCAL`` "unknown‑tool" default — which is meaningless for
    an element label and would wrongly inflate a READ_ONLY scroll, so it is **ignored** (the base wins).
    Escalation only ever fails safe (toward HARD_STOP); it never lowers a tier.
    """
    tier: Tier = classify_gui_action(action)

    labels: list[str] = []
    if action.target:
        labels.append(action.target)
    if real_node is not None:
        label = real_node.name or real_node.role
        if label:
            labels.append(label)

    for label in labels:
        candidate = classify_tool(label, scope=scope)
        if candidate >= Tier.SOCIAL:
            tier = max(tier, candidate)

    return tier


def match_intent(
    intent: FormalizedIntent,
    action: Action,
    pre_frame: Frame,
    *,
    action_tier: Tier | None = None,
    scope: "permissions.Scope | None" = None,
) -> MatchResult:
    """THE deterministic gate: compare the action's real target against the intent's target.

    Steps:
    1. Resolve real node (via ``resolve_action_target``) and intent node (via ``resolve_intent_target``).
    2. Determine effective tier: the self‑computed deterministic floor, raised by an explicit
       ``action_tier`` if the caller (G3) supplies a more precise one. The override may only
       ESCALATE — it can never lower the tier below the floor (fail closed; audit r2).
    3. Compute ``confirmable`` and ``matched``.
    4. Decision table (fail‑closed).

    Never raises.
    """
    real_node = resolve_action_target(action, pre_frame)
    intent_node = resolve_intent_target(intent, pre_frame)

    # The deterministic floor is the trustworthy minimum; an explicit action_tier from the caller may
    # only RAISE it (a richer MCP schema can know an action is MORE dangerous, never less). A too-low
    # override can therefore never silently weaken enforcement on this safety gate. (audit r2.)
    floor = action_effective_tier(action, real_node, scope)
    tier = max(action_tier, floor) if action_tier is not None else floor

    confirmable = (real_node is not None) and (intent_node is not None)
    matched = confirmable and (node_key(real_node) == node_key(intent_node))

    # "Targetless" = the side DECLARED no element to act on (no coord AND no target string / empty
    # intent target). A declared-but-unresolved target (coord hits nothing / intent names a ghost) is
    # NOT targetless — it is unconfirmable and must fail closed, never take the both-targetless
    # ALLOW carve-out. (audit r2: a CLICK into the void + a ghost intent must HARD_STOP, not ALLOW.)
    action_targetless = action.coord is None and not action.target
    intent_targetless = not intent.target

    real_key = node_key(real_node)
    intent_key = node_key(intent_node)

    # Decision table (first match wins, deterministic)
    if matched:
        decision = GateDecision.ALLOW
        reason = "Real target matches intent target."
    elif action_targetless and intent_targetless:
        # Genuinely nothing to match (a viewport scroll / bare keypress with no claimed element).
        if tier >= Tier.FULL_ACCESS:
            decision = GateDecision.HARD_STOP
            reason = "Targetless Full‑Access action cannot be confirmed (fail closed)."
        else:
            decision = GateDecision.ALLOW
            reason = "Both sides targetless; nothing to match (G1 governs the tier interrupt)."
    else:
        # A real desync OR a declared-but-unresolved target → enforce by tier (fail closed).
        if tier >= Tier.WRITE_LOCAL:
            decision = GateDecision.HARD_STOP
            reason = (
                "Desync on a mutating tier (real target != intent-named element)."
                if confirmable
                else "Unconfirmable declared target on a mutating tier (fail closed)."
            )
        else:  # tier == READ_ONLY
            decision = GateDecision.WARN
            reason = (
                "Desync / unconfirmable target on a read‑only action (surface, do not block)."
            )

    return MatchResult(
        decision=decision,
        matched=matched,
        confirmable=confirmable,
        action_tier=tier,
        real_key=real_key,
        intent_key=intent_key,
        reason=reason,
    )


def is_desync(
    intent: FormalizedIntent,
    action: Action,
    pre_frame: Frame,
    *,
    scope: "permissions.Scope | None" = None,
) -> bool:
    """Convenience: returns ``True`` iff ``match_intent(...).decision is GateDecision.HARD_STOP``."""
    return match_intent(intent, action, pre_frame, scope=scope).decision is GateDecision.HARD_STOP
