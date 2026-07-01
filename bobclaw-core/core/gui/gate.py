"""
core/gui/gate.py — MS2-G3 two-tier verify WIRING for the GUI lane.

Composes G1 (``core/gui/tiers.py``: ``Tier`` classification, ``requires_human``) + G2
(``core/gui/intent.py``: ``FormalizedIntent`` / ``match_intent`` / ``GateDecision`` / ``MatchResult``)
into a single deterministic, **no-model**, ≤10 ms **pre-action** gate (Tier-1; DESIGN-MS-D1 §2.1/§3-G3).
The pre-action gate is on the pre-actuation critical path: a ``Full-Access`` tier OR a desync raises the
§2.7 human interrupt (BLOCK) — the action must NOT actuate.

It also provides a Tier-2 escalation adapter (:func:`make_semantic_verifier`) that reuses the MS-2
decorrelated cross-family critic (``core/verify/postcondition.make_postcondition_verifier``) for
*semantic* post-conditions the structural a11y floor can't judge. Per DECISIONS-MS2 the Tier-2 critic
runs **POST-action, off the pre-actuation critical path** — it never gates actuation; it is fail-safe
(``violated``/``unknown``/unreachable → ``ok=False``).

Import-light: the MS-2 critic and the loop flag are LAZY imports inside functions, so importing this
module pulls in no backend, node, HTTP, or model module. Purely additive — it composes the landed
G1/G2/MS-2 primitives and modifies none of them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from core.gui.actions import format_action
from core.gui.intent import FormalizedIntent, GateDecision, MatchResult, match_intent
from core.gui.tiers import Tier, requires_human
from core.gui.types import Action, Frame, Subgoal, Verdict

if TYPE_CHECKING:  # ``Scope`` is referenced only in string-quoted annotations — no runtime import
    import core.permissions as permissions


# ─── Public data model ───────────────────────────────────────────────────────────

class GateAction(str, Enum):
    """The pre-action gate's verdict for one proposed action."""

    PROCEED = "proceed"  # faithful, non-Full-Access — cleared to actuate
    WARN = "warn"        # READ_ONLY desync/unconfirmable — surface, still actuate
    BLOCK = "block"      # Full-Access tier OR a desync — do NOT actuate (§2.7 human interrupt)


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """Outcome of a single pre-action gate check."""

    action: GateAction
    tier: Tier             # the action's EFFECTIVE tier (from G2 match_intent.action_tier)
    match: MatchResult     # the full G2 result (decision/matched/confirmable/keys/reason)
    reason: str

    @property
    def blocked(self) -> bool:
        """True iff the action must NOT actuate (the §2.7 human interrupt)."""
        return self.action is GateAction.BLOCK

    @property
    def warned(self) -> bool:
        """True iff a non-blocking warning was surfaced (READ_ONLY desync)."""
        return self.action is GateAction.WARN

    @property
    def proceed(self) -> bool:
        """True iff the action is cleared with no warning."""
        return self.action is GateAction.PROCEED

    @property
    def actuates(self) -> bool:
        """True iff the action should be actuated (PROCEED or WARN — i.e. not blocked)."""
        return self.action is not GateAction.BLOCK


# ─── Public API ──────────────────────────────────────────────────────────────────

def pre_act_gate(
    intent: FormalizedIntent,
    action: Action,
    pre_frame: Frame,
    *,
    action_tier: Tier | None = None,
    scope: "permissions.Scope | None" = None,
) -> GateOutcome:
    """The Tier-1 pre-action gate (deterministic, no model, ≤10 ms): composes G1 + G2.

    Steps:
        1. ``m = match_intent(intent, action, pre_frame, action_tier=action_tier, scope=scope)`` (G2).
        2. ``tier = m.action_tier`` (G2 already composed G1's by-kind floor + label escalation).
        3. Decision (first match wins, fail-closed):
           - ``m.hard_stop`` → BLOCK (G2 desync/unconfirmable on a mutating tier, or targetless
             Full-Access).
           - ``requires_human(tier)`` (``tier is Tier.FULL_ACCESS``) → BLOCK (G1 — this fires EVEN
             when ``m.decision`` is ALLOW: a *faithful* Full-Access action still needs a human; G2
             detects desync only and never re-implements the Full-Access interrupt).
           - ``m.decision is GateDecision.WARN`` → WARN (READ_ONLY desync — surface, do not block).
           - else (``GateDecision.ALLOW``, non-Full-Access) → PROCEED.

    Over-blocking toward the human is fail-safe; a PROCEED/WARN is provably non-Full-Access (G2
    escalates any dangerous element/target above READ_ONLY first). Never raises (``match_intent`` is
    total).
    """
    m = match_intent(intent, action, pre_frame, action_tier=action_tier, scope=scope)
    tier = m.action_tier

    if m.hard_stop:
        return GateOutcome(
            action=GateAction.BLOCK,
            tier=tier,
            match=m,
            reason=f"desync/unconfirmable hard-stop (do not actuate): {m.reason}",
        )

    if requires_human(tier):
        return GateOutcome(
            action=GateAction.BLOCK,
            tier=tier,
            match=m,
            reason="Full-Access action requires a human (G1); will not actuate without approval.",
        )

    if m.decision is GateDecision.WARN:
        return GateOutcome(action=GateAction.WARN, tier=tier, match=m, reason=m.reason)

    return GateOutcome(action=GateAction.PROCEED, tier=tier, match=m, reason=m.reason)


def make_intent_gate(
    intents: "Mapping[str, FormalizedIntent] | None" = None,
    *,
    scope: "permissions.Scope | None" = None,
) -> "Callable[[Subgoal, Action, Frame], GateOutcome]":
    """Factory: a deterministic ``gate(subgoal, action, frame) -> GateOutcome`` loop seam.

    Per call: look up ``intents.get(subgoal.text)``; if absent, derive a faithful default
    ``FormalizedIntent(target=action.target, kind=action.kind)`` (the action acts on the element it
    names — a coord that physically lands elsewhere then desyncs against this default). Then
    ``pre_act_gate(intent, action, frame, scope=scope)``. ``intents`` is copied defensively;
    pure/deterministic/no-model.
    """
    _intents: dict[str, FormalizedIntent] = dict(intents) if intents else {}

    def gate(subgoal: Subgoal, action: Action, frame: Frame) -> GateOutcome:
        intent = _intents.get(subgoal.text)
        if intent is None:
            intent = FormalizedIntent(target=action.target, kind=action.kind)
        return pre_act_gate(intent, action, frame, scope=scope)

    return gate


def render_frame(frame: Frame) -> str:
    """Deterministic, total text rendering of a post-action ``Frame`` for the critic prompt.

    First line: ``size=<size> image_hash=<hash>``; then one ``- [role] id=... name=... value=...``
    line per a11y node. Pure — never raises.
    """
    lines = [f"size={frame.size} image_hash={frame.image_hash}"]
    for n in frame.a11y:
        lines.append(f"- [{n.role}] id={n.node_id!r} name={n.name!r} value={n.value!r}")
    return "\n".join(lines)


def make_semantic_verifier(
    *,
    actor_backend: str,
    team: str | None = None,
    critic_backend: str | None = None,
    send=None,
    render_result=None,
) -> "Callable[[Subgoal, Action, Frame, Frame, object], Verdict]":
    """Factory: the POST-action Tier-2 escalation adapter (off the pre-actuation critical path).

    Reuses the MS-2 PUBLIC sync adapter ``make_postcondition_verifier`` (lazy import) — it does NOT
    re-implement the decorrelated critic or the async bridge. The returned
    ``verify(subgoal, action, pre_frame, post_frame, diff) -> Verdict`` builds the MS-2 payload
    ``{"step": format_action(action), "statement": subgoal.text,
    "result": render_result(post_frame), "actor_backend": actor_backend}`` and maps the critic's
    pass/fail to a ``Verdict``.

    Decorrelation: ``actor_backend`` is the grounding head's family; MS-2 enforces a different-family
    critic (raises if a same-family ``critic_backend`` is forced). Fail-safe: MS-2 returns ``False`` on
    ``violated`` / ``unknown`` / unreachable / cancelled → the ``Verdict`` is ``ok=False`` (a Tier-2
    critic NEVER auto-passes on ambiguity).
    """
    from core.verify.postcondition import make_postcondition_verifier  # lazy: keep import-light

    _bool = make_postcondition_verifier(team=team, critic_backend=critic_backend, send=send)
    _render = render_result or render_frame

    def verify(
        subgoal: Subgoal,
        action: Action,
        pre_frame: Frame,
        post_frame: Frame,
        diff: object,  # FrameDiff | None — accepted for the loop seam signature, unused by the critic
    ) -> Verdict:
        payload = {
            "step": format_action(action),
            "statement": subgoal.text,
            "result": _render(post_frame),
            "actor_backend": actor_backend,
        }
        try:
            passed = _bool(payload)
        except Exception:  # noqa: BLE001 — fail-safe: any unexpected error never auto-passes
            passed = False
        reason = (
            "tier2-semantic: holds"
            if passed
            else "tier2-semantic: not-satisfied (violated/unknown/unreachable -> fail-safe)"
        )
        return Verdict(ok=passed, reason=reason, criteria=(("tier2-semantic", passed),))

    return verify


# ─── Run-level predicate ─────────────────────────────────────────────────────────

_HUMAN_INTERRUPT_FLAG: str | None = None  # lazily cached from core.gui.loop


def was_human_interrupted(result) -> bool:
    """True iff any step in *result* carries the loop's ``HUMAN_INTERRUPT_FLAG``.

    The clean way for a caller to detect a surfaced §2.7 pre-act interrupt without overloading
    ``RunStatus`` (a locked enum with no "interrupt" member). Lazy-imports the flag from
    ``core.gui.loop`` (avoids a loop↔gate import cycle at module load).
    """
    global _HUMAN_INTERRUPT_FLAG
    if _HUMAN_INTERRUPT_FLAG is None:
        from core.gui.loop import HUMAN_INTERRUPT_FLAG

        _HUMAN_INTERRUPT_FLAG = HUMAN_INTERRUPT_FLAG
    return any(getattr(step, "flag", "") == _HUMAN_INTERRUPT_FLAG for step in result.steps)
