"""Tests for core.gui.surface (Surface ABC contract + FakeSurface state machine)."""
from __future__ import annotations

import pytest

from core.gui import Action, ActionKind, FakeSurface, Surface, format_action, frame_signature
from conftest import frame, node

CLICK_OK = Action(ActionKind.CLICK, target="ok")
CLICK_MENU = Action(ActionKind.CLICK, target="menu")


def _surface():
    s0 = frame("home", node("button", "menu", node_id="menu"))
    s1 = frame("menu", node("button", "settings", node_id="ok"))
    return FakeSurface(
        states={"home": s0, "menu": s1},
        transitions={("home", format_action(CLICK_MENU)): "menu"},
        start="home",
    )


def test_surface_is_abstract():
    with pytest.raises(TypeError):
        Surface()  # cannot instantiate abstract base


def test_start_must_exist():
    with pytest.raises(ValueError):
        FakeSurface(states={"a": frame("a")}, transitions={}, start="missing")


def test_capture_advances_seq_same_signature():
    s = _surface()
    f1 = s.capture()
    f2 = s.capture()
    assert f2.seq > f1.seq
    assert frame_signature(f1) == frame_signature(f2)  # seq ignored


def test_transition_hit_advances_state():
    s = _surface()
    assert s.capture().image_hash == "home"
    res = s.act(CLICK_MENU)
    assert res.performed
    assert s.capture().image_hash == "menu"


def test_transition_miss_is_silent_no_op():
    s = _surface()
    res = s.act(CLICK_OK)  # no transition from home for this action
    assert res.performed and res.error == ""  # actuated, but...
    assert s.capture().image_hash == "home"  # ...nothing changed (silent failure)


def test_inject_error():
    s0 = frame("home")
    s = FakeSurface(
        states={"home": s0},
        transitions={},
        start="home",
        inject_error={format_action(CLICK_OK)},
    )
    res = s.act(CLICK_OK)
    assert not res.performed and res.error.startswith("injected:")


def test_reset_restores_start_and_seq():
    s = _surface()
    s.capture()
    s.act(CLICK_MENU)
    assert s.capture().image_hash == "menu"
    s.reset()
    f = s.capture()
    assert f.image_hash == "home" and f.seq == 1  # seq counter reset, then this capture = 1
