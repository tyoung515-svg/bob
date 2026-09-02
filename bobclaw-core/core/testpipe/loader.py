"""Test-pipe — benchmark loader interface + visible/hidden split (SPEC §9 units 1, 2).

A :class:`BenchmarkPlugin` declares its spine (SPEC §2) and yields
:class:`~core.testpipe.types.TestItem`s. The crux honesty fence (SPEC §7.1) is the
visible/hidden split: :func:`extract_doctests` pulls the docstring ``>>>`` examples
(SAFE to show a model) out of a prompt, so a plugin can separate the visible
examples it flows to the pipe from the hidden test payload the external scorer
holds. Built on stdlib :mod:`doctest` — no network, no dataset download.
"""
from __future__ import annotations

import doctest
import re
from typing import Optional, Protocol, runtime_checkable

from core.testpipe.types import SPINE_BUILD, TestItem


@runtime_checkable
class BenchmarkPlugin(Protocol):
    """The plugin contract (SPEC §9 unit 1). A plugin declares ``name`` + ``spine``
    and loads a list of :class:`TestItem`. The DATASET load may be lazy/guarded (an
    EvalPlus plugin imports the ``evalplus`` package only inside ``load``), but the
    INTERFACE is what the pipe binds to."""

    name: str
    spine: str

    def load(self, limit: Optional[int] = None) -> list[TestItem]:
        ...


# ── visible/hidden split (SPEC §7.1) ────────────────────────────────────────

_ENTRY_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)


def extract_signature(prompt: str) -> str:
    """The ``name(params)`` header from a prompt's ``def`` line, or ``""``.

    Used by the build spine to render a skeleton stub. Tolerant: returns ``""``
    when the prompt carries no ``def`` header (a plugin may then supply one)."""
    m = re.search(r"def\s+([A-Za-z_][A-Za-z0-9_]*\s*\([^\n]*?\))\s*:", prompt, re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def extract_entry_point(prompt: str) -> str:
    """The first ``def`` name in a prompt, or ``""``."""
    m = _ENTRY_RE.search(prompt)
    return m.group(1) if m else ""


def extract_doctests(prompt: str, entry_point: str = "") -> tuple[str, ...]:
    """Docstring ``>>>`` examples → VISIBLE assertion strings (SPEC §7.1).

    Parses the prompt with :class:`doctest.DocTestParser` and turns each example
    that has an expected output into an ``assert <source> == <want>`` string
    referencing the callable. Examples with no expected output (side-effect-only)
    are rendered as a bare call (a smoke that it does not raise). Deterministic +
    order-preserving so re-runs are stable.

    An example is a VISIBLE test by construction — it lives in the shown docstring
    — so this NEVER surfaces a hidden payload; the hidden suite lives outside the
    prompt entirely (SPEC §7.2).
    """
    parser = doctest.DocTestParser()
    try:
        examples = parser.get_examples(prompt)
    except ValueError:
        return ()
    out: list[str] = []
    for ex in examples:
        source = ex.source.strip()
        if not source:
            continue
        want = _clean_want(ex.want or "")
        if want:
            out.append(f"assert {source} == {want}")
        elif source:
            out.append(source)
    return tuple(out)


def _clean_want(want: str) -> str:
    """Trim a doctest's expected output of trailing docstring-delimiter lines.

    Real docstrings (and the fixtures) often close the triple-quote on the line
    directly after the last doctest, and doctest.DocTestParser slurps that same-indent
    line into want. A trailing bare triple-quote delimiter is dropped before stripping.
    """
    lines = (want or "").splitlines()
    while lines and lines[-1].strip() in ('"""', "'''"):
        lines.pop()
    return "\n".join(lines).strip()


def split_visible_hidden(
    prompt: str, full_tests: tuple[str, ...], entry_point: str = ""
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a problem's tests into ``(visible, hidden)`` (SPEC §7.1).

    ``visible`` = the docstring doctests parsed from ``prompt`` (safe to show).
    ``hidden`` = ``full_tests`` with any assertion that is textually identical to a
    visible one removed (so the hidden set is DISJOINT — a doctest that also appears
    in the official suite is not double-counted as hidden). The disjointness is what
    the fence guard (SPEC §7.3) later relies on."""
    visible = extract_doctests(prompt, entry_point)
    vis_norm = {re.sub(r"\s+", "", v) for v in visible}
    hidden = tuple(t for t in full_tests if re.sub(r"\s+", "", t) not in vis_norm)
    return visible, hidden
