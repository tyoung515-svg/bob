"""Tests for core/gui/recovery.py – MS2-G7 stuck-classifier adjudication + matched recovery.

Uses fake async send, no model/Docker/network.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest

from core.gui.types import (
    Action,
    ActionKind,
    A11yNode,
    Frame,
    FrameDiff,
    Verdict,
    FailureType,
    StuckSignal,
    StuckConfig,
)
from core.gui.classify import classify_failure
from core.gui.stuck import StuckDetector
from core.ledger.types import EXHAUSTED_TAG, RETRY_LIMIT
from core.verify.postcondition import family_of, is_decorrelated, PostConditionError

from core.gui.recovery import (
    RecoveryAction,
    RecoveryDecision,
    RecoveryState,
    RecoveryDirective,
    AdjudicationResult,
    RecoveryOutcome,
    MATCHED_RECOVERY,
    TERMINAL_RECOVERIES,
    ADJUDICATION_CATEGORIES,
    matched_recovery,
    should_recover,
    render_failure_evidence,
    build_adjudication_prompt,
    parse_adjudication,
    resolve_adjudicator,
    decorrelated_alternative,
    adjudicate_failure,
    make_failure_adjudicator,
    plan_recovery,
    recover,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _node(role: str, name: str = "", value: str = "",
          node_id: str = "") -> A11yNode:
    return A11yNode(role=role, name=name, value=value, node_id=node_id)


def _frame(image_hash: str, *roles: str, seq: int = 0,
           size: tuple[int, int] = (100, 100)) -> Frame:
    return Frame(seq=seq, size=size, image_hash=image_hash,
                 a11y=tuple(_node(role=r) for r in roles))


def _action(kind: ActionKind = ActionKind.CLICK, target: str = "btn",
            text: str = "", coord: tuple[int, int] | None = (10, 20)) -> Action:
    return Action(kind=kind, target=target, text=text, coord=coord)


def _diff(changed: bool = True, pixel: bool = True, a11y: bool = True,
          added: tuple[str, ...] = (), removed: tuple[str, ...] = (),
          text_changed: bool = False) -> FrameDiff:
    return FrameDiff(changed=changed, pixel_changed=pixel, a11y_changed=a11y,
                     added=added, removed=removed, text_changed=text_changed)


def _verdict(ok: bool = False, reason: str = "failed") -> Verdict:
    return Verdict(ok=ok, reason=reason)


def make_send(reply: str) -> Callable:
    """Return a fake async `send` that returns *reply*."""
    async def send(messages: Any, backend: str) -> str:
        return reply
    return send


def raising_send(msg: str = "net error") -> Callable:
    """Return a fake async `send` that raises RuntimeError."""
    async def send(messages: Any, backend: str) -> str:
        raise RuntimeError(msg)
    return send


# evidence builders for each failure type (deterministic floor)
def _wrong_element_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    action = _action()
    diff = _diff(changed=True)
    verdict = _verdict(ok=False)
    post = _frame("p", "button", "list")  # benign roles
    return action, diff, verdict, post


def _no_state_change_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    action = _action()
    diff = _diff(changed=False)
    verdict = _verdict(ok=False)
    post = _frame("p", "button")
    return action, diff, verdict, post


def _parse_error_evidence() -> tuple[None, FrameDiff, Verdict, Frame]:
    action = None
    diff = _diff()
    verdict = _verdict(ok=False)
    post = _frame("p")
    return action, diff, verdict, post


def _none_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    action = _action()
    diff = _diff()
    verdict = _verdict(ok=True)  # NONE
    post = _frame("p")
    return action, diff, verdict, post


def _modal_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    action = _action()
    diff = _diff()
    verdict = _verdict(ok=False)
    post = _frame("p", "dialog")  # modal trigger
    return action, diff, verdict, post


def _auth_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    action = _action()
    diff = _diff()
    verdict = _verdict(ok=False)
    post = _frame("p", "login")
    return action, diff, verdict, post


def _loading_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    action = _action()
    diff = _diff()
    verdict = _verdict(ok=False)
    post = _frame("p", "spinner")
    return action, diff, verdict, post


def _perception_evidence() -> tuple[Action, FrameDiff, Verdict, Frame]:
    # floor will be WRONG_ELEMENT, but we only use this for matching tests
    return _wrong_element_evidence()


# ---------------------------------------------------------------------------
# 1. matched_recovery table
# ---------------------------------------------------------------------------

def test_matched_recovery_table() -> None:
    """Every FailureType maps to its contracted RecoveryAction."""
    expected = {
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
    for ft, expected_action in expected.items():
        assert MATCHED_RECOVERY[ft] is expected_action, f"MATCHED_RECOVERY[{ft}] mismatch"
        assert matched_recovery(ft) is expected_action, f"matched_recovery({ft}) mismatch"

    assert TERMINAL_RECOVERIES == frozenset({RecoveryAction.ESCALATE_HUMAN, RecoveryAction.ABORT})
    assert matched_recovery(FailureType.WRONG_ELEMENT) is RecoveryAction.RE_BRANCH

# ---------------------------------------------------------------------------
# 2. should_recover
# ---------------------------------------------------------------------------

def test_should_recover() -> None:
    """Deterministic combinations of signal + verdict."""
    # VETO_STREAK always True
    assert should_recover(StuckSignal.VETO_STREAK, None) is True
    assert should_recover(StuckSignal.VETO_STREAK, Verdict(ok=True)) is True
    # NO_PROGRESS always True
    assert should_recover(StuckSignal.NO_PROGRESS, None) is True
    assert should_recover(StuckSignal.NO_PROGRESS, Verdict(ok=True)) is True
    # NONE + failing verdict → True
    assert should_recover(StuckSignal.NONE, Verdict(ok=False)) is True
    # NONE + passing verdict → False
    assert should_recover(StuckSignal.NONE, Verdict(ok=True)) is False
    # NONE + None → False
    assert should_recover(StuckSignal.NONE, None) is False

    # Real StuckDetector veto_streak: distinct frame sigs + action keys so NO_PROGRESS /
    # ACTION_REPEAT do NOT trip first (higher precedence) — isolate the verdict-stream signal.
    detector = StuckDetector(StuckConfig(veto_streak_limit=3))
    for i in range(3):
        detector.record(f"sig{i}", f"key{i}", False)
    signal = detector.check()
    assert signal is StuckSignal.VETO_STREAK
    assert should_recover(signal, Verdict(ok=False)) is True

# ---------------------------------------------------------------------------
# 3. plan_recovery bounded re-branch then surface (ERG reuse)
# ---------------------------------------------------------------------------

def test_plan_recovery_bounded_then_surfaced() -> None:
    """First call RE_BRANCH, second SURFACE, third idempotent SURFACE."""
    st = RecoveryState("save the file")
    actor = "deepseek_v4_flash"

    s1, d1 = plan_recovery(st, FailureType.WRONG_ELEMENT, actor_backend=actor)
    assert d1.decision is RecoveryDecision.RE_BRANCH
    assert d1.surfaced is False
    assert d1.alternative != ""
    assert family_of(d1.alternative) != family_of(actor)
    assert s1.retry_count == 1

    s2, d2 = plan_recovery(s1, FailureType.WRONG_ELEMENT, actor_backend=actor)
    assert d2.decision is RecoveryDecision.SURFACE
    assert d2.surfaced is True
    assert d2.status_tag == EXHAUSTED_TAG
    assert s2.retry_count == 2

    s3, d3 = plan_recovery(s2, FailureType.WRONG_ELEMENT, actor_backend=actor)
    assert d3.decision is RecoveryDecision.SURFACE
    assert d3.surfaced is True  # idempotent terminal

    # ensure alternative is not deepseek family
    assert "deepseek" not in family_of(d1.alternative).lower()

# ---------------------------------------------------------------------------
# 4. plan_recovery terminal actions
# ---------------------------------------------------------------------------

def test_plan_recovery_terminal() -> None:
    st = RecoveryState("test")
    actor = "deepseek_v4_flash"

    # AUTH_BLOCK → ESCALATE_HUMAN, surfaced, no tag, state unchanged
    s, d = plan_recovery(st, FailureType.AUTH_BLOCK, actor_backend=actor)
    assert d.action is RecoveryAction.ESCALATE_HUMAN
    assert d.decision is RecoveryDecision.SURFACE
    assert d.surfaced is True
    assert d.status_tag == ""
    assert s.retry_count == 0  # not consumed

    # IMPOSSIBLE → ABORT, surfaced, EXHAUSTED_TAG
    s, d = plan_recovery(st, FailureType.IMPOSSIBLE, actor_backend=actor)
    assert d.action is RecoveryAction.ABORT
    assert d.decision is RecoveryDecision.SURFACE
    assert d.surfaced is True
    assert d.status_tag == EXHAUSTED_TAG
    assert s.retry_count == 0

    # NONE → NONE action, NONE decision, surfaced False
    s, d = plan_recovery(st, FailureType.NONE, actor_backend=actor)
    assert d.action is RecoveryAction.NONE
    assert d.decision is RecoveryDecision.NONE
    assert d.surfaced is False
    assert s.retry_count == 0  # unchanged

# ---------------------------------------------------------------------------
# 5. plan_recovery matched action carried
# ---------------------------------------------------------------------------

def test_plan_recovery_matched_action() -> None:
    actor = "deepseek_v4_flash"
    st = RecoveryState("sub")

    # PERCEPTION → RECAPTURE
    s, d = plan_recovery(st, FailureType.PERCEPTION, actor_backend=actor)
    assert d.action is RecoveryAction.RECAPTURE
    assert d.decision is RecoveryDecision.RE_BRANCH
    assert family_of(d.alternative) != family_of(actor)

    # LOADING → WAIT_RETRY
    s, d = plan_recovery(st, FailureType.LOADING, actor_backend=actor)
    assert d.action is RecoveryAction.WAIT_RETRY
    assert d.decision is RecoveryDecision.RE_BRANCH

    # MODAL_INTERRUPT → DISMISS_MODAL
    s, d = plan_recovery(st, FailureType.MODAL_INTERRUPT, actor_backend=actor)
    assert d.action is RecoveryAction.DISMISS_MODAL
    assert d.decision is RecoveryDecision.RE_BRANCH

    # GROUNDING_AMBIGUITY → REGROUND
    s, d = plan_recovery(st, FailureType.GROUNDING_AMBIGUITY, actor_backend=actor)
    assert d.action is RecoveryAction.REGROUND
    assert d.decision is RecoveryDecision.RE_BRANCH

# ---------------------------------------------------------------------------
# 6. decorrelated_alternative
# ---------------------------------------------------------------------------

def test_decorrelated_alternative() -> None:
    # deepseek actor → not deepseek family
    alt1 = decorrelated_alternative("deepseek_v4_flash")
    assert "deepseek" not in family_of(alt1).lower()

    # tried excludes that pick
    alt2 = decorrelated_alternative("deepseek_v4_flash", tried=[alt1])
    assert alt2 != alt1
    assert "deepseek" not in family_of(alt2).lower()

    # novel family "holo" → some real backend (not holo)
    alt3 = decorrelated_alternative("holo")
    assert "holo" not in family_of(alt3).lower()

    # deterministic across calls with same args
    alt4 = decorrelated_alternative("deepseek_v4_flash")
    assert alt4 == alt1

# ---------------------------------------------------------------------------
# 7. resolve_adjudicator
# ---------------------------------------------------------------------------

def test_resolve_adjudicator() -> None:
    actor = "deepseek_v4_flash"
    glm = "glm_5_2"

    # explicit adjudicator_backend returned as-is
    assert resolve_adjudicator(actor, adjudicator_backend=glm) == glm

    # without argument → decorrelated (cross‑family)
    res = resolve_adjudicator(actor)
    assert is_decorrelated(actor, res) is True

    # forced same‑family raises PostConditionError
    with pytest.raises(PostConditionError):
        resolve_adjudicator(actor, adjudicator_backend="deepseek_v4_flash")

# ---------------------------------------------------------------------------
# 8. parse_adjudication
# ---------------------------------------------------------------------------

def test_parse_adjudication() -> None:
    # valid JSON
    cat, reasons = parse_adjudication('{"category":"modal_interrupt","reasons":["a modal blocked it"]}')
    assert cat is FailureType.MODAL_INTERRUPT
    assert reasons == ["a modal blocked it"]

    # fenced with ```json
    cat, reasons = parse_adjudication('```json\n{"category":"loading"}\n```')
    assert cat is FailureType.LOADING
    assert reasons == []

    # out‑of‑menu category → None
    cat, reasons = parse_adjudication('{"category":"banana"}')
    assert cat is None
    assert isinstance(reasons, list)

    # "none" is not in ADJUDICATION_CATEGORIES → None
    cat, reasons = parse_adjudication('{"category":"none"}')
    assert cat is None

    # not JSON → None
    cat, reasons = parse_adjudication("this is not json")
    assert cat is None

# ---------------------------------------------------------------------------
# 9. adjudicate_failure failsafe (fake send)
# ---------------------------------------------------------------------------

def test_adjudicate_failure_failsafe() -> None:
    actor = "deepseek_v4_flash"
    glm = "glm_5_2"
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)
    assert floor is FailureType.WRONG_ELEMENT

    # successful adjudication
    result = asyncio.run(adjudicate_failure(
        action, diff, verdict, post,
        actor_backend=actor,
        send=make_send('{"category":"modal_interrupt"}'),
        adjudicator_backend=glm
    ))
    assert result.adjudicated is True
    assert result.failure is FailureType.MODAL_INTERRUPT
    assert result.backend == glm
    assert result.decorrelated is True

    # raising send → floor
    result = asyncio.run(adjudicate_failure(
        action, diff, verdict, post,
        actor_backend=actor,
        send=raising_send(),
        adjudicator_backend=glm
    ))
    assert result.adjudicated is False
    assert result.failure is floor

    # garbage send → floor
    result = asyncio.run(adjudicate_failure(
        action, diff, verdict, post,
        actor_backend=actor,
        send=make_send("garbage"),
        adjudicator_backend=glm
    ))
    assert result.adjudicated is False
    assert result.failure is floor

    # same‑family adjudicator → floor (fail‑safe, no raise)
    result = asyncio.run(adjudicate_failure(
        action, diff, verdict, post,
        actor_backend=actor,
        send=make_send('{"category":"modal_interrupt"}'),
        adjudicator_backend=actor  # same family – fails safe
    ))
    assert result.adjudicated is False
    assert result.failure is floor

    # NONE floor → no send call, failure NONE
    calls = []
    async def tracking_send(messages, backend):
        calls.append(backend)
        return '{"category":"modal_interrupt"}'
    action2, diff2, verdict2, post2 = _none_evidence()
    result = asyncio.run(adjudicate_failure(
        action2, diff2, verdict2, post2,
        actor_backend=actor,
        send=tracking_send,
        adjudicator_backend=glm
    ))
    assert result.failure is FailureType.NONE
    assert result.adjudicated is False
    assert calls == []  # send never called

# ---------------------------------------------------------------------------
# 10. make_failure_adjudicator (sync bridge, fake send)
# ---------------------------------------------------------------------------

def test_make_failure_adjudicator_sync() -> None:
    actor = "deepseek_v4_flash"
    glm = "glm_5_2"
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)
    assert floor is FailureType.WRONG_ELEMENT

    # HOLDS‑style category send
    adj = make_failure_adjudicator(
        actor_backend=actor,
        adjudicator_backend=glm,
        send=make_send('{"category":"loading"}')
    )
    result = adj(action, diff, verdict, post)
    assert result.adjudicated is True
    assert result.failure is FailureType.LOADING

    # raising send → floor
    adj = make_failure_adjudicator(
        actor_backend=actor,
        adjudicator_backend=glm,
        send=raising_send()
    )
    result = adj(action, diff, verdict, post)
    assert result.adjudicated is False
    assert result.failure is floor

# ---------------------------------------------------------------------------
# 11. recover – floor byte‑identical when adjudicator None / mocked
# ---------------------------------------------------------------------------

def _run_recover_floor(evidence: tuple, actor: str = "deepseek_v4_flash"):
    action, diff, verdict, post = evidence
    state = RecoveryState("sub")
    outcome_none = recover(state, action=action, diff=diff, verdict=verdict, post=post,
                          actor_backend=actor, adjudicator=None)
    floor = classify_failure(action, diff, verdict, post)
    assert outcome_none[1].adjudication.failure is floor

    # adjudicator that raises → same floor
    adj_raise = make_failure_adjudicator(actor_backend=actor,
                                         adjudicator_backend="glm_5_2",
                                         send=raising_send())
    outcome_raise = recover(state, action=action, diff=diff, verdict=verdict, post=post,
                            actor_backend=actor, adjudicator=adj_raise)
    assert outcome_raise[1].adjudication.failure is floor

    # directive matches plan_recovery for the floor
    _, direct = plan_recovery(state, floor, actor_backend=actor)
    assert outcome_none[1].directive.action is direct.action

def test_recover_floor_byte_identical() -> None:
    # NO_STATE_CHANGE
    _run_recover_floor(_no_state_change_evidence())
    # WRONG_ELEMENT
    _run_recover_floor(_wrong_element_evidence())
    # PARSE_ERROR (action None)
    _run_recover_floor(_parse_error_evidence())
    # MODAL_INTERRUPT
    _run_recover_floor(_modal_evidence())
    # AUTH_BLOCK
    _run_recover_floor(_auth_evidence())
    # LOADING
    _run_recover_floor(_loading_evidence())

# ---------------------------------------------------------------------------
# 12. recover – adjudicated override drives matched recovery
# ---------------------------------------------------------------------------

def test_recover_adjudicated_override() -> None:
    actor = "deepseek_v4_flash"
    glm = "glm_5_2"
    action, diff, verdict, post = _wrong_element_evidence()  # floor = WRONG_ELEMENT
    floor = classify_failure(action, diff, verdict, post)
    assert floor is FailureType.WRONG_ELEMENT

    # adjudicator returns MODAL_INTERRUPT
    adj = make_failure_adjudicator(
        actor_backend=actor,
        adjudicator_backend=glm,
        send=make_send('{"category":"modal_interrupt"}')
    )
    state = RecoveryState("sub")
    new_state, outcome = recover(state, action=action, diff=diff, verdict=verdict, post=post,
                                 actor_backend=actor, adjudicator=adj)
    assert outcome.adjudication.adjudicated is True
    assert outcome.adjudication.failure is FailureType.MODAL_INTERRUPT
    assert outcome.directive.action is RecoveryAction.DISMISS_MODAL
    # recover() must PASS THROUGH plan_recovery's bounded state (audit r3): the bounded retry was
    # consumed (DISMISS_MODAL is a retryable RE_BRANCH), so retry_count advanced and the decorrelated
    # alternative was recorded — matching plan_recovery for the adjudicated category exactly.
    exp_state, exp_directive = plan_recovery(state, FailureType.MODAL_INTERRUPT, actor_backend=actor)
    assert new_state == exp_state
    assert new_state.retry_count == 1
    assert new_state.tried and family_of(new_state.tried[-1]) != family_of(actor)
    assert outcome.directive.decision is exp_directive.decision is RecoveryDecision.RE_BRANCH

# ---------------------------------------------------------------------------
# 13. import purity
# ---------------------------------------------------------------------------

def test_import_purity() -> None:
    src_path = Path(__file__).resolve().parents[2] / "core/gui/recovery.py"
    text = src_path.read_text(encoding="utf-8")
    # Never-present anywhere (no HTTP / backend / docker dependency at all).
    forbidden = ("core.backends", "aiohttp", "requests", "httpx", "import docker")
    for item in forbidden:
        assert item not in text, f"Forbidden import in recovery.py: {item}"
    # core.nodes may appear ONLY inside the lazy _default_send real-send seam (MS-2 pattern),
    # never at module top-level. Assert it is absent from the top-level import block; the
    # subprocess sys.modules probe below is the binding "not loaded at import" guarantee.
    top_block = text.split("logger = logging.getLogger", 1)[0]
    assert "core.nodes" not in top_block, "core.nodes must not be a top-level import"

    # subprocess import probe
    probe_code = """
import sys
import core.gui.recovery
print(any(m in sys.modules for m in ('core.backends', 'core.nodes')))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe_code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        env={"PYTHONPATH": ".", **{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}},
        timeout=10
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "core.backends or core.nodes were loaded at import"


# ---------------------------------------------------------------------------
# 14. audit r1 hardening — no untried decorrelated alternative -> surface (never a
#     re-branch with an empty alternative; never an unbounded/dropped failure)
# ---------------------------------------------------------------------------

def test_decorrelated_alternative_skips_all_tried_raises() -> None:
    """When every reachable cross-family backend is already tried, raise (no untried alt)."""
    actor = "deepseek_v4_flash"
    # all cross-family backends in DEFAULT_CRITIC_PREFERENCE for a deepseek actor
    all_cross = ["glm_5_2", "minimax", "kimi_code", "claude_api", "gemini_pro"]
    with pytest.raises(PostConditionError):
        decorrelated_alternative(actor, tried=all_cross)


def test_plan_recovery_surfaces_when_no_decorrelated_alternative(monkeypatch) -> None:
    """If no untried decorrelated alternative exists, plan_recovery SURFACES (does not
    re-branch with an empty alternative) — bounded->surfaced contract stays airtight."""
    import core.gui.recovery as rec

    def _no_alt(*a, **k):
        raise PostConditionError("no untried decorrelated alternative")

    monkeypatch.setattr(rec, "decorrelated_alternative", _no_alt)
    st = RecoveryState("save the file")
    s, d = rec.plan_recovery(st, FailureType.WRONG_ELEMENT, actor_backend="deepseek_v4_flash")
    assert d.decision is RecoveryDecision.SURFACE
    assert d.surfaced is True
    assert d.status_tag == EXHAUSTED_TAG
    assert d.alternative == ""          # never a re-branch with an empty alternative
    assert s == st                      # state unchanged (no retry consumed)
    assert d.action is RecoveryAction.RE_BRANCH  # the matched action for WRONG_ELEMENT is preserved


def test_adjudicate_failure_render_error_falls_back(monkeypatch) -> None:
    """A render/prompt-build error inside adjudicate_failure -> the deterministic floor (no raise).

    Audit r2 hardening: the evidence-render + prompt-build are inside the fail-safe try block, so
    even a contrived render explosion never raises out of the adjudicator.
    """
    import core.gui.recovery as rec

    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)

    def _boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(rec, "render_failure_evidence", _boom)
    res = asyncio.run(rec.adjudicate_failure(
        action, diff, verdict, post,
        actor_backend="deepseek_v4_flash",
        send=make_send('{"category":"modal_interrupt"}'),
        adjudicator_backend="glm_5_2",
    ))
    assert res.adjudicated is False
    assert res.failure is floor


# ---------------------------------------------------------------------------
# 15. audit r5 hardening
# ---------------------------------------------------------------------------

def test_build_adjudication_prompt_injects_untrusted_evidence_last() -> None:
    """A page-controlled evidence string containing literal {floor}/{evidence} tokens is preserved
    verbatim (NOT re-substituted) — the trusted {floor} is substituted first, untrusted last."""
    evidence = "elem name is {floor} and also {evidence} literally"
    out = build_adjudication_prompt(evidence, FailureType.WRONG_ELEMENT)
    assert evidence in out                       # untrusted tokens survived verbatim
    assert 'floor of "wrong_element"' in out     # the header {floor} placeholder WAS substituted
    assert 'floor of "{floor}"' not in out


def test_recover_adjudicator_raises_falls_back() -> None:
    """A misbehaving INJECTED adjudicator must never crash recover -> deterministic floor."""
    actor = "deepseek_v4_flash"
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)

    def boom_adjudicator(a, d, v, p):
        raise RuntimeError("custom adjudicator exploded")

    new_state, outcome = recover(RecoveryState("sub"), action=action, diff=diff, verdict=verdict,
                                 post=post, actor_backend=actor, adjudicator=boom_adjudicator)
    assert outcome.adjudication.adjudicated is False
    assert outcome.adjudication.failure is floor
    # the directive still follows plan_recovery for the floor (recovery proceeds, never crashes)
    _, exp = plan_recovery(RecoveryState("sub"), floor, actor_backend=actor)
    assert outcome.directive.action is exp.action


def test_adjudicate_failure_out_of_menu_category_falls_back() -> None:
    """A parseable JSON whose category is OUTSIDE the menu (none/audit_veto) -> floor, not adjudicated."""
    actor, glm = "deepseek_v4_flash", "glm_5_2"
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)
    for bad in ('{"category":"audit_veto"}', '{"category":"none"}'):
        res = asyncio.run(adjudicate_failure(
            action, diff, verdict, post, actor_backend=actor,
            send=make_send(bad), adjudicator_backend=glm))
        assert res.adjudicated is False, bad
        assert res.failure is floor, bad


def test_adjudicate_failure_cancelled_send_falls_back() -> None:
    """A send raising asyncio.CancelledError is caught -> floor (does NOT propagate / crash)."""
    actor, glm = "deepseek_v4_flash", "glm_5_2"
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)

    async def cancel_send(messages, backend):
        raise asyncio.CancelledError()

    res = asyncio.run(adjudicate_failure(
        action, diff, verdict, post, actor_backend=actor,
        send=cancel_send, adjudicator_backend=glm))
    assert res.adjudicated is False
    assert res.failure is floor


def test_recover_terminal_adjudication_surfaces() -> None:
    """recover with an adjudicated TERMINAL category surfaces (no retry consumed) — through recover."""
    actor, glm = "deepseek_v4_flash", "glm_5_2"
    action, diff, verdict, post = _wrong_element_evidence()  # floor WRONG_ELEMENT (retryable)
    st = RecoveryState("sub")
    # AUTH_BLOCK adjudication -> ESCALATE_HUMAN, surfaced, state unchanged
    adj_auth = make_failure_adjudicator(actor_backend=actor, adjudicator_backend=glm,
                                        send=make_send('{"category":"auth_block"}'))
    ns, oc = recover(st, action=action, diff=diff, verdict=verdict, post=post,
                     actor_backend=actor, adjudicator=adj_auth)
    assert oc.adjudication.adjudicated is True
    assert oc.directive.action is RecoveryAction.ESCALATE_HUMAN
    assert oc.directive.decision is RecoveryDecision.SURFACE and oc.directive.surfaced is True
    assert ns == st  # terminal: no retry consumed
    # IMPOSSIBLE adjudication -> ABORT, surfaced with EXHAUSTED_TAG
    adj_imp = make_failure_adjudicator(actor_backend=actor, adjudicator_backend=glm,
                                       send=make_send('{"category":"impossible"}'))
    ns2, oc2 = recover(st, action=action, diff=diff, verdict=verdict, post=post,
                       actor_backend=actor, adjudicator=adj_imp)
    assert oc2.directive.action is RecoveryAction.ABORT
    assert oc2.directive.surfaced is True and oc2.directive.status_tag == EXHAUSTED_TAG
    assert ns2 == st


# ---------------------------------------------------------------------------
# 16. audit r6 hardening
# ---------------------------------------------------------------------------

def test_recover_adjudicator_returns_non_result_falls_back() -> None:
    """A seam that returns a NON-AdjudicationResult (no .failure) fails safe to the floor (mirror G3)."""
    actor = "deepseek_v4_flash"
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)

    def bad_adjudicator(a, d, v, p):
        return "not an AdjudicationResult"  # malformed seam return -> AttributeError on .failure

    ns, oc = recover(RecoveryState("sub"), action=action, diff=diff, verdict=verdict, post=post,
                     actor_backend=actor, adjudicator=bad_adjudicator)
    assert oc.adjudication.adjudicated is False
    assert oc.adjudication.failure is floor
    assert oc.directive.action is matched_recovery(floor)


def test_make_failure_adjudicator_cancelled_bridge_falls_back(monkeypatch) -> None:
    """A CancelledError out of the _run_blocking bridge falls back to the floor (mirror MS-2 sync adapter)."""
    import core.verify.postcondition as pc

    def _cancel(_make_coro):
        raise asyncio.CancelledError()

    # patch BEFORE the factory binds _run_blocking via its lazy import
    monkeypatch.setattr(pc, "_run_blocking", _cancel)
    action, diff, verdict, post = _wrong_element_evidence()
    floor = classify_failure(action, diff, verdict, post)
    adj = make_failure_adjudicator(actor_backend="deepseek_v4_flash", adjudicator_backend="glm_5_2",
                                   send=make_send('{"category":"loading"}'))
    res = adj(action, diff, verdict, post)
    assert res.adjudicated is False
    assert res.failure is floor
