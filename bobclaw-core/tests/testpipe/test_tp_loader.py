"""Test-pipe — loader interface + visible/hidden split (SPEC §9 units 1, 2)."""
from __future__ import annotations

from core.testpipe.fixtures import FixtureBenchmark, fixture_items
from core.testpipe.loader import (
    BenchmarkPlugin,
    extract_doctests,
    extract_entry_point,
    extract_signature,
    split_visible_hidden,
)
from core.testpipe.types import SPINE_BUILD, TestItem


def test_extract_signature_and_entry_point():
    prompt = 'def foo(a, b=1, *c):\n    """doc"""\n'
    assert extract_signature(prompt) == "foo(a, b=1, *c)"
    assert extract_entry_point(prompt) == "foo"
    assert extract_signature("no def here") == ""


def test_extract_doctests_strips_closing_delimiter():
    # The closing triple-quote on the line after the doctest must NOT bleed into want.
    prompt = 'def add(a, b):\n    """Sum.\n\n    >>> add(1, 2)\n    3\n    """\n'
    assert extract_doctests(prompt, "add") == ("assert add(1, 2) == 3",)


def test_extract_doctests_side_effect_example_is_bare_call():
    prompt = 'def go():\n    """\n    >>> go()\n    """\n'
    assert extract_doctests(prompt, "go") == ("go()",)


def test_split_visible_hidden_is_disjoint():
    prompt = 'def add(a, b):\n    """\n    >>> add(1, 2)\n    3\n    """\n'
    full = ("assert add(1, 2) == 3", "assert add(-1, 1) == 0")
    visible, hidden = split_visible_hidden(prompt, full, "add")
    assert visible == ("assert add(1, 2) == 3",)
    # the visible doctest is removed from hidden (disjoint by construction, §7.1)
    assert hidden == ("assert add(-1, 1) == 0",)


def test_fixture_items_have_clean_split():
    items = fixture_items()
    assert [i.id for i in items] == ["fx_add", "fx_is_even", "fx_reverse", "fx_count_vowels"]
    for it in items:
        assert it.visible_tests and it.hidden_tests
        # disjoint (whitespace-normalized): no visible test is also a hidden test
        vis = {v.replace(" ", "") for v in it.visible_tests}
        hid = {h.replace(" ", "") for h in it.hidden_tests}
        assert not (vis & hid), f"{it.id}: visible/hidden overlap"


def test_fixture_benchmark_conforms_to_plugin_protocol():
    plugin = FixtureBenchmark()
    assert isinstance(plugin, BenchmarkPlugin)   # runtime Protocol check
    assert plugin.name == "fixture" and plugin.spine == SPINE_BUILD
    assert len(plugin.load(limit=2)) == 2
    assert all(isinstance(i, TestItem) for i in plugin.load())
