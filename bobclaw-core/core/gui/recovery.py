# core/gui/recovery.py - MS2-G7 stuck-classifier model adjudication + matched-recovery router
# + bounded re-branch-then-surface (ERG shape applied to GUI).
#
# DESIGN-MS-D1 sec3-G7: a model adjudication layer on the deterministic stuck/classify floor;
# matched recovery (perception/grounding-ambiguity/modal/auth/loading/impossible);
# consumes the Tier-2 verdict stream (VETO_STREAK) as a trip signal;
# the ERG re-branch/bound/surface shape applied to GUI;
# reuses MS-2 decorrelated routing + core/ledger/erg.py;
# the deterministic floor is the fail-safe fallback.
#
# Composes read-only: core/gui/classify.classify_failure, core/verify/postcondition.*,
# core/ledger/erg.on_entailment_failure, core/ledger.types.*.
# Does not modify any of those files.

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, Sequence

from core.gui.types import (
    Action,
    FailureType,
    Frame,
    FrameDiff,
    StuckSignal,
    Verdict,
)
from core.gui.classify import classify_failure
from core.ledger.erg import on_entailment_failure
from core.ledger.types import EXHAUSTED_TAG, RETRY_LIMIT, ErgAction
from core.verify.postcondition import (
    DEFAULT_CRITIC_PREFERENCE,
    PostConditionError,
    decorrelated_critic_backend,
    family_of,
    is_decorrelated,
)

logger = logging.getLogger(__name__)


# -- Matched-recovery taxonomy (ready-gate a - deterministic, no model) ----------

class RecoveryAction(str, Enum):
    """The matched recovery action for a failure type."""

    NONE = "none"
    RECAPTURE = "recapture"
    REGROUND = "reground"
    DISMISS_MODAL = "dismiss_modal"
    WAIT_RETRY = "wait_retry"
    RE_BRANCH = "re_branch"
    ESCALATE_HUMAN = "escalate_human"
    ABORT = "abort"


MATCHED_RECOVERY: dict[FailureType, RecoveryAction] = {
    FailureType.NONE: RecoveryAction.NONE,
    FailureType.PERCEPTION: RecoveryAction.RECAPTURE,
    FailureType.GROUNDING_AMBIGUITY: RecoveryAction.REGROUND,
    FailureType.PARSE_ERROR: RecoveryAction.REGROUND,
    FailureType.MODAL_INTERRUPT: RecoveryAction.DISMISS_MODAL,
    FailureType.AUTH_BLOCK: RecoveryAction.ESCALATE_HUMAN,
    FailureType.LOADING: RecoveryAction.WAIT_RETRY,
    FailureType.IMPOSSIBLE: RecoveryAction.ABORT,
    FailureType.NO_STATE_CHANGE: RecoveryAction.RE_BRANCH,
    FailureType.WRONG_ELEMENT: RecoveryAction.RE_BRANCH,
    FailureType.AUDIT_VETO: RecoveryAction.RE_BRANCH,
}

TERMINAL_RECOVERIES: frozenset[RecoveryAction] = frozenset(
    {RecoveryAction.ESCALATE_HUMAN, RecoveryAction.ABORT}
)


def matched_recovery(failure: FailureType) -> RecoveryAction:
    """Total lookup over MATCHED_RECOVERY; unmapped -> RE_BRANCH."""
    return MATCHED_RECOVERY.get(failure, RecoveryAction.RE_BRANCH)


# -- Trip signal - consume the Tier-2 verdict stream (the sec7<-sec6 wiring) ----------

def should_recover(
    stuck_signal: StuckSignal, verdict: Verdict | None
) -> bool:
    """True iff stuck_signal is a non-NONE trip or verdict is a failure.

    Pure - no side effects.
    """
    if stuck_signal not in (None, StuckSignal.NONE):
        return True
    if verdict is not None and not verdict.ok:
        return True
    return False


# -- Model adjudication data classes ---------------------------------------------

@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    """Outcome of a model adjudication over a step failure.

    On every non-success path (model unavailable, parse failure, out-of-menu category)
    ``failure`` equals the deterministic ``floor`` and ``adjudicated`` is False.
    """

    failure: FailureType
    floor: FailureType
    adjudicated: bool
    backend: str = ""
    decorrelated: bool = False
    reason: str = ""


class RecoveryDecision(str, Enum):
    """Outcome of plan_recovery for a given failure."""

    NONE = "none"
    RE_BRANCH = "re_branch"
    SURFACE = "surface"


@dataclass(frozen=True, slots=True)
class RecoveryDirective:
    """The recovery action determined by plan_recovery."""

    action: RecoveryAction
    failure: FailureType
    decision: RecoveryDecision
    alternative: str = ""
    status_tag: str = ""
    constraint: str = ""
    surfaced: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryState:
    """Mutable state tracking retry count and tried alternatives for a subgoal."""

    subgoal: str
    retry_count: int = 0
    tried: tuple[str, ...] = ()
    status: str = "PENDING"


# -- Model adjudication over the deterministic floor (ready-gates c, d) ----------

ADJUDICATION_CATEGORIES: tuple[FailureType, ...] = (
    FailureType.PERCEPTION,
    FailureType.GROUNDING_AMBIGUITY,
    FailureType.MODAL_INTERRUPT,
    FailureType.AUTH_BLOCK,
    FailureType.LOADING,
    FailureType.IMPOSSIBLE,
    FailureType.NO_STATE_CHANGE,
    FailureType.WRONG_ELEMENT,
    FailureType.PARSE_ERROR,
)

_ADJUDICATION_PROMPT_TEMPLATE: str = """You are a failure adjudicator for a GUI automation system.

The deterministic classifier returned a floor of "{floor}".

Below is the evidence for the failed step:

{evidence}

Your task: classify the failure into EXACTLY ONE of the following categories (the exact enum value):

- perception
- grounding_ambiguity
- modal_interrupt
- auth_block
- loading
- impossible
- no_state_change
- wrong_element
- parse_error

Brief meaning per category:
- perception: the agent perceived the state incorrectly (e.g. screenshot interpretation error).
- grounding_ambiguity: the action could not be grounded (no matching element).
- modal_interrupt: a dialog/modal/alert appeared and blocked the action.
- auth_block: a login/signin/auth screen appeared.
- loading: a progress spinner/loading indicator is present.
- impossible: the action cannot be performed (e.g. infinite loop, contradiction).
- no_state_change: nothing changed after the action (silent input, dead click).
- wrong_element: something changed but the expected post-condition was not met.
- parse_error: the action description was malformed.

Reply with a SINGLE line of JSON in exactly this format and nothing else:
{"category":"<one of the above>","reasons":["short reason", "..."]}"""


def render_failure_evidence(
    action: Action | None,
    diff: FrameDiff | None,
    verdict: Verdict | None,
    post: Frame,
) -> str:
    """Deterministic, total text rendering of a failed step for the adjudicator.

    Never raises; handles None arguments gracefully.
    """
    lines: list[str] = []
    # Action fields
    if action is not None:
        lines.append(
            f"[Action] kind={action.kind.value} target={action.target!r} "
            f"text={action.text!r} key={action.key!r} coord={action.coord}"
        )
    else:
        lines.append("[Action] (none)")
    # Diff fields
    if diff is not None:
        lines.append(
            f"[Diff] changed={diff.changed} added={diff.added!r} "
            f"removed={diff.removed!r} text_changed={diff.text_changed}"
        )
    else:
        lines.append("[Diff] (none)")
    # Verdict fields
    if verdict is not None:
        lines.append(f"[Verdict] ok={verdict.ok} reason={verdict.reason!r}")
    else:
        lines.append("[Verdict] (none)")
    # Post-frame
    lines.append(f"[Post] size={post.size}")
    for node in post.a11y:
        lines.append(
            f"  - role={node.role!r} name={node.name!r} value={node.value!r}"
        )
    return "\n".join(lines)


def build_adjudication_prompt(evidence: str, floor: FailureType) -> str:
    """Brace-safe prompt for the adjudicator (uses str.replace, not str.format).

    Substitutes the TRUSTED {floor} token FIRST, then injects the UNTRUSTED {evidence} LAST, so a
    page-controlled a11y name/value inside `evidence` that happens to contain the literal text
    "{floor}" / "{evidence}" is NEVER re-substituted (audit r5 hardening).
    """
    return (
        _ADJUDICATION_PROMPT_TEMPLATE.replace("{floor}", floor.value).replace(
            "{evidence}", evidence
        )
    )


def parse_adjudication(raw: str) -> tuple[FailureType | None, list[str]]:
    """Extract a JSON object from the model reply and map to a FailureType.

    Returns (None, [reason]) on any parse failure or category outside
    ADJUDICATION_CATEGORIES.
    """
    # Strip optional markdown fences
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE).strip()

    # Try to locate a JSON object
    obj = None
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            pass
    if obj is None:
        # Search for a balanced JSON object
        brace_depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        break
                    except json.JSONDecodeError:
                        pass
    if not isinstance(obj, dict):
        return (None, ["parse_error: could not extract JSON object"])

    category = obj.get("category")
    raw_reasons = obj.get("reasons")
    reasons: list[str] = []
    if isinstance(raw_reasons, list):
        reasons = [str(r) for r in raw_reasons]
    elif raw_reasons:
        reasons = [str(raw_reasons)]

    if not category:
        return (None, reasons or ["parse_error: missing 'category'"])

    try:
        ft = FailureType(category)
    except ValueError:
        return (None, reasons or [f"parse_error: unknown category {category!r}"])

    if ft not in ADJUDICATION_CATEGORIES:
        return (None, reasons or [f"parse_error: category {ft.value!r} not in adjudication menu"])

    return (ft, reasons)


def resolve_adjudicator(
    actor_backend: str,
    *,
    adjudicator_backend: str | None = None,
    team: str | None = None,
) -> str:
    """Resolve an adjudicator backend decorrelated from the actor.

    Raises PostConditionError if the resulting backend is not decorrelated.
    """
    crit = adjudicator_backend or decorrelated_critic_backend(
        actor_backend, team=team
    )
    if not is_decorrelated(actor_backend, crit):
        raise PostConditionError(
            f"adjudicator {crit!r} not decorrelated from actor {actor_backend!r}"
        )
    return crit


def decorrelated_alternative(
    actor_backend: str,
    tried: Sequence[str] = (),
    *,
    candidates: Sequence[str] | None = None,
    team: str | None = None,
) -> str:
    """Return a first backend from (candidates + DEFAULT_CRITIC_PREFERENCE)
    that is not in tried and whose family differs from the actor's.

    Falls back to decorrelated_critic_backend (the MS-2 guarantee, which also consults a
    team-bound critic). Raises PostConditionError when there is no UNTRIED decorrelated
    alternative (so plan_recovery surfaces, never re-branches without a real alternative).
    Never returns a same-family backend, never returns a backend already in *tried*.
    """
    tried_set = set(tried or ())
    actor_family = family_of(actor_backend)
    pool: list[str] = list(candidates or []) + list(DEFAULT_CRITIC_PREFERENCE)
    for c in pool:
        if c and c not in tried_set and family_of(c) != actor_family:
            return c
    # Fallback: the MS-2 guarantee (cross-family, also consults the team critic). If even that
    # only yields an already-tried backend, there is no UNTRIED decorrelated alternative left ->
    # raise so the caller surfaces (honoring the "skip tried" + "re-branch to a NEW alt" contract).
    fb = decorrelated_critic_backend(actor_backend, team=team, candidates=candidates)
    if fb in tried_set:
        raise PostConditionError(
            f"no untried decorrelated alternative for actor {actor_backend!r} "
            f"(tried={sorted(tried_set)})"
        )
    return fb


async def _default_send(messages: list[dict], backend: str) -> str:
    """Lazy real-backend send (imported only when called)."""
    from core.nodes.execute import _send_to_backend

    return await _send_to_backend(messages, backend)


async def adjudicate_failure(
    action: Action | None,
    diff: FrameDiff | None,
    verdict: Verdict | None,
    post: Frame,
    *,
    actor_backend: str,
    send: Callable[[list[dict], str], Awaitable[str]] | None = None,
    adjudicator_backend: str | None = None,
    team: str | None = None,
) -> AdjudicationResult:
    """FAIL-SAFE async adjudication: never raises out.

    On any failure (no decorrelated critic, send error, cancellation, unparseable,
    out-of-menu category) returns the deterministic floor with adjudicated=False.
    """
    floor = classify_failure(action, diff, verdict, post)
    if floor is FailureType.NONE:
        return AdjudicationResult(
            failure=FailureType.NONE,
            floor=FailureType.NONE,
            adjudicated=False,
            reason="no failure",
        )

    try:
        crit = resolve_adjudicator(
            actor_backend, adjudicator_backend=adjudicator_backend, team=team
        )
    except Exception:  # noqa: BLE001
        # DELIBERATE: catch Exception, NOT BaseException. KeyboardInterrupt / SystemExit MUST
        # propagate (never swallow a Ctrl-C / interpreter shutdown); CancelledError is handled
        # explicitly at the send site below. This mirrors core/verify/postcondition.py's posture.
        # No decorrelated critic -> deterministic floor fallback.
        return AdjudicationResult(
            failure=floor,
            floor=floor,
            adjudicated=False,
            reason="no decorrelated adjudicator available",
        )

    # Prompt build + send are ALL inside the try so adjudicate_failure is airtight fail-safe:
    # any exception (a malformed-frame render, a prompt-build error, or a send failure) -> the
    # deterministic floor, never a raise out (audit r2 hardening).
    send_fn = send or _default_send
    try:
        evidence = render_failure_evidence(action, diff, verdict, post)
        prompt = build_adjudication_prompt(evidence, floor)
        messages = [
            {"role": "system", "content": "You are an impartial failure adjudicator."},
            {"role": "user", "content": prompt},
        ]
        raw = await send_fn(messages, crit)
    except asyncio.CancelledError:  # noqa: BLE001
        # Cancellation is not a crash, but we still fall back to floor
        return AdjudicationResult(
            failure=floor,
            floor=floor,
            adjudicated=False,
            backend=crit,
            decorrelated=True,
            reason="adjudication cancelled",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("adjudication send failed (backend=%r): %s", crit, exc)
        return AdjudicationResult(
            failure=floor,
            floor=floor,
            adjudicated=False,
            backend=crit,
            decorrelated=True,
            reason=f"send_error: {type(exc).__name__}: {exc}",
        )

    cat, reasons = parse_adjudication(raw)
    if cat is None:
        return AdjudicationResult(
            failure=floor,
            floor=floor,
            adjudicated=False,
            backend=crit,
            decorrelated=True,
            reason="; ".join(reasons) if reasons else "unparseable adjudication",
        )

    return AdjudicationResult(
        failure=cat,
        floor=floor,
        adjudicated=True,
        backend=crit,
        decorrelated=True,
        reason="; ".join(reasons) if reasons else "adjudicated",
    )


def make_failure_adjudicator(
    *,
    actor_backend: str,
    team: str | None = None,
    adjudicator_backend: str | None = None,
    send: Callable[[list[dict], str], Awaitable[str]] | None = None,
) -> Callable[[Action | None, FrameDiff | None, Verdict | None, Frame], AdjudicationResult]:
    """Return a SYNC adjudicate(action, diff, verdict, post) -> AdjudicationResult.

    Drives adjudicate_failure via the lazy _run_blocking bridge. Any bridge failure
    returns the deterministic floor (never raises into the caller).
    """
    from core.verify.postcondition import _run_blocking

    def adjudicate(
        action: Action | None,
        diff: FrameDiff | None,
        verdict: Verdict | None,
        post: Frame,
    ) -> AdjudicationResult:
        try:
            return _run_blocking(
                lambda: adjudicate_failure(
                    action,
                    diff,
                    verdict,
                    post,
                    actor_backend=actor_backend,
                    send=send,
                    adjudicator_backend=adjudicator_backend,
                    team=team,
                )
            )
        except asyncio.CancelledError as exc:  # noqa: BLE001
            # Cancellation falls back to the floor at the sync boundary too (mirrors MS-2's
            # make_postcondition_verifier sync adapter: never auto-pass, never abort) - audit r6.
            logger.warning("sync adjudicator bridge cancelled: %s", exc)
            floor = classify_failure(action, diff, verdict, post)
            return AdjudicationResult(
                failure=floor, floor=floor, adjudicated=False,
                reason="sync bridge cancelled; floor fallback",
            )
        except Exception as exc:  # noqa: BLE001
            # DELIBERATE: Exception, not BaseException (KeyboardInterrupt/SystemExit propagate).
            # The bridge itself should not raise, but defend against any unexpected error -> floor.
            logger.warning("sync adjudicator bridge failed: %s", exc)
            floor = classify_failure(action, diff, verdict, post)
            return AdjudicationResult(
                failure=floor,
                floor=floor,
                adjudicated=False,
                reason="sync bridge error; floor fallback",
            )

    return adjudicate


# -- Bounded re-branch-then-surface (ready-gate b - ERG reuse) ------------------

def plan_recovery(
    state: RecoveryState,
    failure: FailureType,
    *,
    actor_backend: str,
    alternatives: Sequence[str] | None = None,
    team: str | None = None,
) -> tuple[RecoveryState, RecoveryDirective]:
    """Bounded re-branch-then-surface state machine.

    Two ORTHOGONAL fields are returned on the RecoveryDirective:
      * ``action``   = the matched recovery MECHANISM (RECAPTURE / REGROUND / DISMISS_MODAL /
                       WAIT_RETRY / RE_BRANCH / ESCALATE_HUMAN / ABORT) -- WHAT corrective step to
                       take, from the MATCHED_RECOVERY table. Always preserved.
      * ``decision`` = the ERG BOUND state (NONE / RE_BRANCH / SURFACE) -- whether a bounded
                       attempt remains (RE_BRANCH) or the failure is now surfaced (SURFACE).
    EVERY non-terminal recovery is uniformly ERG-bounded (so NO retryable recovery -- not even a
    WAIT_RETRY / RECAPTURE -- can loop forever); the matched mechanism stays in ``action`` while
    ``decision`` tracks the bound. A terminal recovery (auth/impossible) surfaces immediately.

    First-match wins:
    1. failure is NONE -> nothing to recover.
    2. matched recovery is terminal -> surface immediately, no retry consumed.
    3. else -> compose ERG's on_entailment_failure for the bounded retryable recovery.
    """
    # 1. No failure
    if failure is FailureType.NONE:
        return (
            state,
            RecoveryDirective(
                action=RecoveryAction.NONE,
                failure=FailureType.NONE,
                decision=RecoveryDecision.NONE,
                reason="no failure, no recovery",
            ),
        )

    matched = matched_recovery(failure)
    # 2. Terminal recovery
    if matched in TERMINAL_RECOVERIES:
        status_tag = ""
        reason = ""
        if matched is RecoveryAction.ABORT:
            status_tag = EXHAUSTED_TAG
            reason = "impossible failure; aborting"
        elif matched is RecoveryAction.ESCALATE_HUMAN:
            reason = "auth block; escalating to human (never auto-bypass)"
        return (
            state,  # state unchanged
            RecoveryDirective(
                action=matched,
                failure=failure,
                decision=RecoveryDecision.SURFACE,
                surfaced=True,
                status_tag=status_tag,
                reason=reason,
            ),
        )

    # 3. Retryable recovery
    try:
        alt = decorrelated_alternative(
            actor_backend, state.tried, candidates=alternatives, team=team
        )
    except PostConditionError:
        # No UNTRIED decorrelated alternative exists -> we cannot re-branch "to a decorrelated
        # alternative" meaningfully. Surface immediately as a known-failure (the bounded->surfaced
        # contract holds: NEVER a re-branch without a real decorrelated alternative, never a silent
        # drop, never a loop). State is unchanged (no retry was actually attempted).
        return (
            state,
            RecoveryDirective(
                action=matched,
                failure=failure,
                decision=RecoveryDecision.SURFACE,
                status_tag=EXHAUSTED_TAG,
                surfaced=True,
                reason="no untried decorrelated alternative; surfacing known failure",
            ),
        )

    entry = {
        "retry_count": state.retry_count,
        "tried_sources": list(state.tried),
        "bid_key": state.subgoal,
        "status": state.status,
    }
    new_source = alt  # always a real, untried, decorrelated backend (decorrelated_alternative guarantee)
    res = on_entailment_failure(entry, new_source)
    e = res["entry"]
    d = res["directive"]

    new_state = RecoveryState(
        subgoal=state.subgoal,
        retry_count=e["retry_count"],
        tried=tuple(e["tried_sources"]),
        status=e["status"],
    )

    if d["action"] == ErgAction.RE_BRANCH.value:
        directive = RecoveryDirective(
            action=matched,
            failure=failure,
            decision=RecoveryDecision.RE_BRANCH,
            alternative=alt,
            constraint=d.get("constraint", ""),
            surfaced=False,
            reason="bounded re-branch to decorrelated alternative",
        )
    else:  # EXHAUSTED_SEARCH
        directive = RecoveryDirective(
            action=matched,
            failure=failure,
            decision=RecoveryDecision.SURFACE,
            status_tag=d.get("status_tag", EXHAUSTED_TAG),
            surfaced=True,
            reason="bounded recovery exhausted; surfacing known failure",
        )

    return (new_state, directive)


# -- Orchestrator ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Combined result of recover(): adjudication + the planned directive."""

    adjudication: AdjudicationResult
    directive: RecoveryDirective


def recover(
    state: RecoveryState,
    *,
    action: Action | None,
    diff: FrameDiff | None,
    verdict: Verdict | None,
    post: Frame,
    actor_backend: str,
    adjudicator: Callable[
        [Action | None, FrameDiff | None, Verdict | None, Frame],
        AdjudicationResult,
    ]
    | None = None,
    alternatives: Sequence[str] | None = None,
    team: str | None = None,
) -> tuple[RecoveryState, RecoveryOutcome]:
    """Compose adjudication (or floor fallback) with plan_recovery.

    If adjudicator is None, uses the deterministic floor (byte-identical).
    """
    floor = classify_failure(action, diff, verdict, post)
    if adjudicator is None:
        adj_result = AdjudicationResult(
            failure=floor,
            floor=floor,
            adjudicated=False,
            reason="deterministic floor (no adjudicator)",
        )
    else:
        # FAIL-SAFE seam (mirrors the G3 loop, which reads _outcome.blocked INSIDE its try): a
        # misbehaving INJECTED adjudicator must NEVER crash recover -> fall back to the floor. The
        # .failure access is INSIDE the try so a non-AdjudicationResult return (AttributeError) also
        # fails safe. The shipped make_failure_adjudicator is already fail-safe; this defends a custom
        # callable too (audit r6).
        try:
            adj_result = adjudicator(action, diff, verdict, post)
            _ = adj_result.failure  # validate the seam returned a usable AdjudicationResult
        except Exception as exc:  # noqa: BLE001 - Exception, not BaseException (KI/SystemExit propagate)
            logger.warning("recover: injected adjudicator misbehaved; floor fallback: %s", exc)
            adj_result = AdjudicationResult(
                failure=floor,
                floor=floor,
                adjudicated=False,
                reason=f"adjudicator misbehaved -> floor: {type(exc).__name__}: {exc}",
            )

    state2, directive = plan_recovery(
        state,
        adj_result.failure,
        actor_backend=actor_backend,
        alternatives=alternatives,
        team=team,
    )
    return (state2, RecoveryOutcome(adjudication=adj_result, directive=directive))
