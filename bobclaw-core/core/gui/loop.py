from __future__ import annotations

"""
Core GUI loop: drives plan execution against a Surface using a Grounder.
Deterministic, pure, no I/O or model calls (deferred to injected seams).
"""

from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

from core.gui.types import (
    Action,
    FailureType,
    Frame,
    FrameDiff,
    Postcondition,
    RunResult,
    RunStatus,
    StepRecord,
    StuckConfig,
    StuckSignal,
    Subgoal,
    Verdict,
)
from core.gui.actions import format_action, parse_action, validate_action
from core.gui.classify import classify_failure
from core.gui.framediff import frame_diff, frame_signature
from core.gui.stuck import StuckDetector
from core.gui.surface import Surface
from core.gui.verify import verify_postcondition

if TYPE_CHECKING:  # annotations only — no runtime import (loop stays import-light + cycle-free)
    from collections.abc import Callable

    from core.gui.gate import GateOutcome


@runtime_checkable
class Grounder(Protocol):
    """Protocol for mapping a textual subgoal + frame to an Action (or None)."""

    def ground(self, subgoal: str, frame: Frame) -> Action | None: ...


class ScriptedGrounder:
    """Deterministic Grounder that looks up subgoals in a static mapping.

    If the mapping value is a string it is parsed via ``parse_action``;
    otherwise it must be an ``Action`` instance.
    """

    def __init__(self, mapping: dict[str, Action | str]) -> None:
        self._mapping: dict[str, Action | str] = mapping

    def ground(self, subgoal: str, frame: Frame) -> Action | None:
        v = self._mapping.get(subgoal)
        if v is None:
            return None
        if isinstance(v, str):
            return parse_action(v)
        return v


# Absolute, config-independent ceiling on total steps — a termination backstop so the
# loop can NEVER spin forever even if a StuckConfig disables every limit (max_steps<=0,
# huge no_change/dedup/veto limits). The StuckDetector's STEP_BUDGET is the normal bound;
# this is the hard floor under it.
_ABSOLUTE_MAX_STEPS = 10_000

# MS2-G3: the flag a blocked StepRecord carries when the pre-act gate raises the §2.7 human interrupt
# (the loop OWNS the flag it writes; core.gui.gate.was_human_interrupted reads it). RunStatus has no
# "interrupt" member (locked enum), so this flag — surfaced on the final StepRecord — is the
# authoritative signal that the run was halted pre-actuation awaiting a human, not stuck/failed at a task.
HUMAN_INTERRUPT_FLAG = "human-interrupt"


class GuiLoop:
    """Main loop that executes a sequence of subgoals against a Surface.

    Uses a Grounder to resolve subgoal text into actions, verifies postconditions
    deterministically, and relies on the StuckDetector for termination conditions.
    """

    def __init__(
        self,
        surface: Surface,
        grounder: Grounder,
        *,
        cfg: StuckConfig = StuckConfig(),
        time_fn=None,
        gate: "Callable[[Subgoal, Action, Frame], GateOutcome] | None" = None,
        semantic_verifier: "Callable[[Subgoal, Action, Frame, Frame, FrameDiff | None], Verdict] | None" = None,
    ) -> None:
        self._surface = surface
        self._grounder = grounder
        self._cfg = cfg
        self._time_fn = time_fn
        # MS2-G3 injected seams (both default None ⇒ the loop path is byte-identical to the skeleton):
        #   gate              — Tier-1 pre-act decision (G1 tier + G2 anti-desync); deterministic/no-model.
        #   semantic_verifier — Tier-2 escalation to the MS-2 decorrelated critic; POST-action, off the
        #                       pre-actuation critical path (DECISIONS-MS2: Tier-2 never gates actuation).
        self._gate = gate
        self._semantic_verifier = semantic_verifier

    def run(self, plan: Sequence[Subgoal]) -> RunResult:
        """Execute the plan, returning a RunResult with step records and outcome."""
        detector = StuckDetector(self._cfg, time_fn=self._time_fn)
        detector.start()

        steps: list[StepRecord] = []
        idx: int = 0
        completed: int = 0
        stuck_signal: StuckSignal = StuckSignal.NONE

        for sg in plan:
            # Inner loop: keep attempting this subgoal until success, stuck, or failure.
            while True:
                if idx >= _ABSOLUTE_MAX_STEPS:
                    # config-independent hard backstop (audit fix): never spin forever.
                    return RunResult(
                        status=RunStatus.STUCK,
                        steps=tuple(steps),
                        stuck_signal=StuckSignal.STEP_BUDGET,
                        completed=completed,
                        total=len(plan),
                    )
                frame = self._surface.capture()
                action = self._grounder.ground(sg.text, frame)

                if action is None:
                    # Grounding failed – cannot act.
                    record = StepRecord(
                        idx=idx,
                        subgoal=sg.text,
                        action=None,
                        pre_hash=frame.image_hash,
                        post_hash=frame.image_hash,
                        diff=None,
                        verdict=None,
                        failure=FailureType.GROUNDING_AMBIGUITY,
                        flag="try-alt",
                    )
                    steps.append(record)
                    detector.record(frame_signature(frame), None, False)
                    idx += 1
                    sig = detector.check()
                    if sig != StuckSignal.NONE:
                        stuck_signal = sig
                        return RunResult(
                            status=RunStatus.STUCK,
                            steps=tuple(steps),
                            stuck_signal=stuck_signal,
                            completed=completed,
                            total=len(plan),
                        )
                    break  # cannot retry this subgoal
                else:
                    # Action produced – validate and execute.
                    ok, why = validate_action(action)
                    if not ok:
                        post = frame
                        diff: FrameDiff | None = None
                        verdict = Verdict(ok=False, reason=why)
                        failure = FailureType.WRONG_ELEMENT
                    else:
                        # MS2-G3 Tier-1 pre-act gate (guarded): when wired, run the deterministic
                        # G1-tier + G2-anti-desync decision BEFORE actuating. A Full-Access tier OR a
                        # desync raises the §2.7 human interrupt: SURFACE it — never reach surface.act.
                        if self._gate is not None:
                            try:
                                _outcome = self._gate(sg, action, frame)
                                _blocked, _reason = _outcome.blocked, _outcome.reason
                            except Exception as exc:  # noqa: BLE001 — a misbehaving gate FAILS CLOSED:
                                # over-block to a human interrupt rather than actuate an unvetted action.
                                _blocked = True
                                _reason = (
                                    "pre-act gate raised — failing closed to a human interrupt: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                            if _blocked:
                                steps.append(
                                    StepRecord(
                                        idx=idx,
                                        subgoal=sg.text,
                                        action=action,
                                        pre_hash=frame.image_hash,
                                        post_hash=frame.image_hash,  # never actuated → post == pre
                                        diff=None,                   # no actuation → no diff
                                        verdict=Verdict(ok=False, reason=_reason),
                                        failure=FailureType.AUTH_BLOCK,
                                        flag=HUMAN_INTERRUPT_FLAG,
                                    )
                                )
                                return RunResult(
                                    status=RunStatus.STUCK,
                                    steps=tuple(steps),
                                    stuck_signal=StuckSignal.NONE,
                                    completed=completed,
                                    total=len(plan),
                                )
                        result = self._surface.act(action)
                        post = self._surface.capture()
                        diff = frame_diff(frame, post)
                        verdict = verify_postcondition(
                            sg.postcondition, frame, post, diff
                        )
                        # MS2-G3 Tier-2 escalation (guarded, POST-action, OFF the pre-actuation path):
                        # when a verifier is wired AND the action actuated AND the deterministic
                        # structural floor produced no judgeable criteria (a semantic-only post-condition
                        # a11y can't express), escalate to the MS-2 decorrelated cross-family critic. With
                        # NO verifier wired this guard short-circuits on `is not None` and the failure
                        # classification (elif/else) is the byte-identical skeleton logic, same order.
                        if (
                            self._semantic_verifier is not None
                            and result.performed
                            and not verdict.criteria
                        ):
                            try:
                                verdict = self._semantic_verifier(sg, action, frame, post, diff)
                            except Exception as exc:  # noqa: BLE001 — a misbehaving verifier FAILS SAFE:
                                # never auto-pass; record a not-ok verdict and the cross-family veto type.
                                verdict = Verdict(
                                    ok=False,
                                    reason=f"tier2-verifier raised: {type(exc).__name__}: {exc}",
                                )
                            # AUDIT_VETO = the reserved §4 cross-family-critic veto type.
                            failure = FailureType.NONE if verdict.ok else FailureType.AUDIT_VETO
                        elif not result.performed:
                            failure = FailureType.NO_STATE_CHANGE
                        else:
                            failure = classify_failure(action, diff, verdict, post)

                    flag = (
                        "dont-retry"
                        if failure
                        in (FailureType.IMPOSSIBLE, FailureType.AUTH_BLOCK)
                        else ""
                    )
                    record = StepRecord(
                        idx=idx,
                        subgoal=sg.text,
                        action=action,
                        pre_hash=frame.image_hash,
                        post_hash=post.image_hash,
                        diff=diff,
                        verdict=verdict,
                        failure=FailureType.NONE if verdict.ok else failure,
                        flag=flag,
                    )
                    steps.append(record)
                    detector.record(
                        frame_signature(post),
                        format_action(action),
                        verdict.ok,
                    )
                    idx += 1
                    sig = detector.check()
                    if sig != StuckSignal.NONE:
                        stuck_signal = sig
                        return RunResult(
                            status=RunStatus.STUCK,
                            steps=tuple(steps),
                            stuck_signal=stuck_signal,
                            completed=completed,
                            total=len(plan),
                        )
                    if verdict.ok:
                        completed += 1
                        break  # subgoal succeeded; move to next
                    # Otherwise continue retrying this subgoal

        # All subgoals processed
        status = RunStatus.COMPLETED if completed == len(plan) else RunStatus.FAILED
        return RunResult(
            status=status,
            steps=tuple(steps),
            stuck_signal=StuckSignal.NONE,
            completed=completed,
            total=len(plan),
        )
