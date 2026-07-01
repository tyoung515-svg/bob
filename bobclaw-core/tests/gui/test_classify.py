"""Tests for core.gui.classify (deterministic failure taxonomy floor)."""
from __future__ import annotations

from core.gui import (
    Action,
    ActionKind,
    FailureType,
    Verdict,
    classify_failure,
    frame_diff,
)
from conftest import frame, node

CLICK = Action(ActionKind.CLICK, target="x")
OK = Verdict(ok=True, reason="", criteria=(("changed", True),))
BAD = Verdict(ok=False, reason="failed: changed", criteria=(("changed", False),))


def test_parse_error_when_action_none():
    post = frame("img")
    assert classify_failure(None, None, None, post) == FailureType.PARSE_ERROR


def test_none_when_verdict_ok():
    post = frame("img")
    d = frame_diff(None, post)
    assert classify_failure(CLICK, d, OK, post) == FailureType.NONE


def test_modal_takes_precedence_over_auth_and_loading_across_nodes():
    # auth node listed first, modal node second -> category precedence picks MODAL
    post = frame("img", node("loginForm"), node("dialog"), node("spinner"))
    d = frame_diff(None, post)
    assert classify_failure(CLICK, d, BAD, post) == FailureType.MODAL_INTERRUPT


def test_auth_then_loading_precedence():
    post = frame("img", node("spinner"), node("signin-box"))
    d = frame_diff(None, post)
    assert classify_failure(CLICK, d, BAD, post) == FailureType.AUTH_BLOCK
    post2 = frame("img", node("progressbar"))
    d2 = frame_diff(None, post2)
    assert classify_failure(CLICK, d2, BAD, post2) == FailureType.LOADING


def test_no_state_change():
    f = frame("img", node("button", "ok"))
    d = frame_diff(f, frame("img", node("button", "ok")))  # identical -> not changed
    assert classify_failure(CLICK, d, BAD, f) == FailureType.NO_STATE_CHANGE


def test_wrong_element_when_changed_but_failed():
    prev = frame("a", node("button", "ok"))
    post = frame("b", node("button", "ok"))  # pixel changed, no recognizable block role
    d = frame_diff(prev, post)
    assert classify_failure(CLICK, d, BAD, post) == FailureType.WRONG_ELEMENT


def test_impossible_when_no_diff_info():
    post = frame("img")
    assert classify_failure(CLICK, None, BAD, post) == FailureType.IMPOSSIBLE
