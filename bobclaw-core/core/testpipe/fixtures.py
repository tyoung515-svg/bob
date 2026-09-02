"""Test-pipe — a tiny in-tree fixture benchmark (SPEC §9 / MS5-P1 scope).

Four hand-authored :class:`TestItem`s with docstring ``>>>`` doctests (⇒ VISIBLE)
and an official hidden suite. This is the network-free stand-in for HumanEval+ so
the WHOLE scaffold (loader → split → chunk → build/council spine → scorer → ledger)
can be exercised end-to-end with NO dataset download and NO live model calls. The
problems are real + runnable, so the subprocess scorer genuinely executes them.

Deliberately trivial by design: the pipe's PLUMBING is what MS5-P1 proves; the real
benchmark microscope (SPEC §11 weak-worker headroom) is the deferred LIVE sweep.
"""
from __future__ import annotations

from core.testpipe.loader import (
    extract_entry_point,
    extract_signature,
    split_visible_hidden,
)
from core.testpipe.types import SPINE_BUILD, TestItem

# Each entry: (id, prompt-with-doctest, full official test suite, canonical solution).
_RAW: list[tuple[str, str, tuple[str, ...], str]] = [
    (
        "fx_add",
        'def add(a, b):\n    """Return the sum of a and b.\n\n    >>> add(1, 2)\n    3\n    """\n',
        ("assert add(1, 2) == 3", "assert add(-1, 1) == 0", "assert add(0, 0) == 0",
         "assert add(100, -50) == 50"),
        "def add(a, b):\n    return a + b\n",
    ),
    (
        "fx_is_even",
        'def is_even(n):\n    """Return True iff n is even.\n\n    >>> is_even(4)\n    True\n    """\n',
        ("assert is_even(4) == True", "assert is_even(3) == False",
         "assert is_even(0) == True", "assert is_even(-2) == True"),
        "def is_even(n):\n    return n % 2 == 0\n",
    ),
    (
        "fx_reverse",
        'def reverse_string(s):\n    """Return s reversed.\n\n    >>> reverse_string("ab")\n    \'ba\'\n    """\n',
        ('assert reverse_string("abc") == "cba"', 'assert reverse_string("") == ""',
         'assert reverse_string("racecar") == "racecar"'),
        "def reverse_string(s):\n    return s[::-1]\n",
    ),
    (
        "fx_count_vowels",
        'def count_vowels(s):\n    """Count the vowels (aeiou) in s.\n\n    >>> count_vowels("hello")\n    2\n    """\n',
        ('assert count_vowels("hello") == 2', 'assert count_vowels("xyz") == 0',
         'assert count_vowels("aeiou") == 5'),
        "def count_vowels(s):\n    return sum(1 for c in s if c in 'aeiou')\n",
    ),
]


def fixture_items() -> list[TestItem]:
    """The 4 fixture problems as :class:`TestItem`s, visible/hidden split via the
    real :func:`split_visible_hidden` (so the split path itself is exercised)."""
    items: list[TestItem] = []
    for item_id, prompt, full_tests, canon in _RAW:
        entry = extract_entry_point(prompt)
        visible, hidden = split_visible_hidden(prompt, full_tests, entry)
        items.append(
            TestItem(
                id=item_id,
                prompt=prompt,
                entry_point=entry,
                visible_tests=visible,
                hidden_tests=hidden,
                signature=extract_signature(prompt),
                canonical_solution=canon,
            )
        )
    return items


class FixtureBenchmark:
    """A :class:`~core.testpipe.loader.BenchmarkPlugin` over the fixture items."""

    name = "fixture"
    spine = SPINE_BUILD

    def load(self, limit=None) -> list[TestItem]:
        items = fixture_items()
        return items if limit is None else items[:limit]
