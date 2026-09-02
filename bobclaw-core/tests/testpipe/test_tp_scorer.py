"""Test-pipe — external scorer bridge (SPEC §7.2, §9 unit 7)."""
from __future__ import annotations

import core.config as cfg
import pytest

from core.testpipe.fixtures import fixture_items
from core.testpipe.scorer import (
    CallableScorer,
    Scorer,
    SubprocessScorer,
    write_hidden_suite,
)


@pytest.fixture(autouse=True)
def _force_subprocess(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "subprocess")   # no Docker dep in-suite


def _add():
    return fixture_items()[0]


async def test_subprocess_scorer_passes_correct_impl(tmp_path):
    scorer = SubprocessScorer(tmp_path)
    assert isinstance(scorer, Scorer)
    res = await scorer.score(_add(), "def add(a, b):\n    return a + b")
    assert res.hidden_pass and res.failed == 0 and res.passed >= 1


async def test_subprocess_scorer_fails_wrong_impl(tmp_path):
    scorer = SubprocessScorer(tmp_path)
    res = await scorer.score(_add(), "def add(a, b):\n    return 0")
    assert not res.hidden_pass and res.failed >= 1


async def test_subprocess_scorer_no_impl_fails(tmp_path):
    res = await SubprocessScorer(tmp_path).score(_add(), None)
    assert not res.hidden_pass


async def test_callable_scorer_mocks_execution():
    scorer = CallableScorer(lambda item, impl: impl is not None)
    assert (await scorer.score(_add(), "x")).hidden_pass
    assert not (await scorer.score(_add(), None)).hidden_pass


def test_write_hidden_suite_isolates_hidden_tests(tmp_path):
    # The ONLY place hidden tests are materialised is a sandbox FILE — never a prompt.
    it = _add()
    write_hidden_suite(tmp_path, it, "def add(a, b):\n    return a + b")
    hidden_file = (tmp_path / "tests" / "test_hidden.py").read_text(encoding="utf-8")
    for t in it.hidden_tests:
        assert t in hidden_file
    assert (tmp_path / "solution.py").exists()
