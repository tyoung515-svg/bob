"""Tests for core.gui.actions (parse / validate / format, the PARSE_ERROR probe)."""
from __future__ import annotations

import pytest

from core.gui import Action, ActionKind, format_action, parse_action, validate_action


def test_parse_basic_kinds():
    assert parse_action("click(target=submit)").kind == ActionKind.CLICK
    assert parse_action("TYPE(text=hello)").kind == ActionKind.TYPE  # case-insensitive
    assert parse_action("noop()").kind == ActionKind.NOOP


def test_parse_all_arg_types():
    a = parse_action("scroll(direction=down, amount=3)")
    assert a.direction == "down" and a.amount == 3
    a = parse_action("click(coord=(12,34))")
    assert a.coord == (12, 34)
    a = parse_action("click(coord=(5,6))")  # coord requires parens (bare comma collides with arg split)
    assert a.coord == (5, 6)
    a = parse_action('type(text="hello world")')
    assert a.text == "hello world"
    a = parse_action("key(key=enter)")
    assert a.key == "enter"


@pytest.mark.parametrize("bad", [
    "frobnicate(x=1)",     # unknown kind
    "click(target=x",      # unbalanced paren
    "click target=x)",     # no opening paren
    "click(bogus=1)",      # unknown arg
    "scroll(amount=two)",  # non-int amount
    "click(coord=1,2,3)",  # bad coord
    "click(coord=x,y)",    # non-int coord
    'type(text="oops)',    # unbalanced quote
    "click(target)",       # missing '='
    "",                    # empty
])
def test_parse_malformed_returns_none(bad):
    assert parse_action(bad) is None


def test_validate_action_per_kind():
    assert validate_action(Action(ActionKind.CLICK, target="x"))[0]
    assert validate_action(Action(ActionKind.CLICK, coord=(1, 2)))[0]
    assert not validate_action(Action(ActionKind.CLICK))[0]
    assert not validate_action(Action(ActionKind.TYPE))[0]
    assert validate_action(Action(ActionKind.TYPE, text="hi"))[0]
    assert validate_action(Action(ActionKind.SCROLL, direction="up"))[0]
    assert not validate_action(Action(ActionKind.SCROLL, direction="sideways"))[0]
    assert validate_action(Action(ActionKind.KEY, key="esc"))[0]
    assert not validate_action(Action(ActionKind.KEY))[0]
    assert validate_action(Action(ActionKind.NOOP))[0]


def test_validate_reason_present_on_failure():
    ok, reason = validate_action(Action(ActionKind.TYPE))
    assert not ok and reason


def test_format_action_canonical_sorted():
    a = Action(ActionKind.SCROLL, direction="down", amount=2)
    # args sorted alphabetically: amount before direction
    assert format_action(a) == "scroll(amount=2, direction=down)"
    assert format_action(Action(ActionKind.NOOP)) == "noop()"


def test_format_action_only_non_default_fields():
    a = Action(ActionKind.CLICK, target="ok")
    assert format_action(a) == "click(target=ok)"
    assert "text=" not in format_action(a)


@pytest.mark.parametrize("a", [
    Action(ActionKind.CLICK, target="submit"),
    Action(ActionKind.TYPE, text="hello"),
    Action(ActionKind.SCROLL, direction="up", amount=5),
    Action(ActionKind.KEY, key="enter"),
    Action(ActionKind.CLICK, coord=(10, 20)),
    Action(ActionKind.NOOP),
])
def test_format_parse_roundtrip(a):
    assert parse_action(format_action(a)) == a


@pytest.mark.parametrize("a", [
    Action(ActionKind.TYPE, text="a, b"),               # comma
    Action(ActionKind.CLICK, target="weird(value)"),    # parens
    Action(ActionKind.TYPE, text='he said "hi"'),       # embedded double quotes
    Action(ActionKind.TYPE, text="  pad  "),            # edge whitespace
    Action(ActionKind.TYPE, text="back\\slash"),        # backslash
    Action(ActionKind.TYPE, text="a=b"),                # equals
])
def test_format_parse_roundtrip_special_values(a):
    # audit fix: values carrying delimiters/whitespace are quoted+escaped so they round-trip
    assert parse_action(format_action(a)) == a


def test_format_action_is_stable_dedup_key():
    a1 = Action(ActionKind.CLICK, target="x")
    a2 = Action(ActionKind.CLICK, target="x")
    assert format_action(a1) == format_action(a2)
    assert format_action(Action(ActionKind.CLICK, target="y")) != format_action(a1)
