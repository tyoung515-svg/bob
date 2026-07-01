"""Tests for core.gui.verify (Default-FAIL semantic post-condition)."""
from __future__ import annotations

from core.gui import Postcondition, frame_diff, verify_postcondition
from conftest import frame, node


def _diff(prev, post):
    return frame_diff(prev, post)


def test_empty_postcondition_is_default_fail():
    f = frame("img", node("button", "ok"))
    pc = Postcondition(expect_changed=False)  # no criteria at all
    v = verify_postcondition(pc, f, f, _diff(f, f))
    assert not v.ok
    assert v.reason == "no postcondition criteria"
    assert v.criteria == ()


def test_expect_changed():
    f1 = frame("a", node("x"))
    f2 = frame("b", node("x"))
    v = verify_postcondition(Postcondition(expect_changed=True), f1, f2, _diff(f1, f2))
    assert v.ok
    v = verify_postcondition(Postcondition(expect_changed=True), f1, f1, _diff(f1, f1))
    assert not v.ok and "changed" in v.reason


def test_present_and_absent():
    prev = frame("a", node("button", "ok", node_id="b1"))
    post = frame("b", node("label", "done", node_id="msg"))
    pc = Postcondition(expect_changed=False, present=("msg",), absent=("b1",))
    v = verify_postcondition(pc, prev, post, _diff(prev, post))
    assert v.ok
    # if the expected node is missing -> fail, reason names it
    pc2 = Postcondition(expect_changed=False, present=("missing",))
    v2 = verify_postcondition(pc2, prev, post, _diff(prev, post))
    assert not v2.ok and "present:missing" in v2.reason


def test_text_in():
    post = frame("b", node("textbox", "field", value="hello world", node_id="f1"))
    pc = Postcondition(expect_changed=False, text_in=(("f1", "world"),))
    assert verify_postcondition(pc, None, post, _diff(None, post)).ok
    pc_bad = Postcondition(expect_changed=False, text_in=(("f1", "absent"),))
    assert not verify_postcondition(pc_bad, None, post, _diff(None, post)).ok


def test_empty_keys_fail_closed():
    # audit fix: a degenerate empty key must never trivially pass (fail closed)
    post = frame("b", node("label", "done", node_id="msg"))
    d = _diff(None, post)
    assert not verify_postcondition(Postcondition(expect_changed=False, present=("",)), None, post, d).ok
    assert not verify_postcondition(Postcondition(expect_changed=False, absent=("",)), None, post, d).ok
    assert not verify_postcondition(Postcondition(expect_changed=False, text_in=(("", "done"),)), None, post, d).ok


def test_all_criteria_must_hold():
    prev = frame("a")
    post = frame("b", node("label", "done", node_id="msg"))
    # changed passes, but present:missing fails -> overall fail
    pc = Postcondition(expect_changed=True, present=("msg", "missing"))
    v = verify_postcondition(pc, prev, post, _diff(prev, post))
    assert not v.ok
    # criteria carried for auditability
    names = [c[0] for c in v.criteria]
    assert "changed" in names and "present:msg" in names and "present:missing" in names
