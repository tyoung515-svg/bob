"""Tests for core.gui.stuck (deterministic predicates + StuckDetector)."""
from __future__ import annotations

from core.gui import (
    StuckConfig,
    StuckDetector,
    StuckSignal,
    action_repeat_count,
    frame_repeat_count,
    over_step_budget,
    over_time_budget,
    veto_streak,
)
from conftest import FakeClock


def test_frame_repeat_count():
    assert frame_repeat_count([]) == 0
    assert frame_repeat_count(["a"]) == 1
    assert frame_repeat_count(["a", "b", "b", "b"]) == 3
    assert frame_repeat_count(["b", "b", "a"]) == 1


def test_action_repeat_count_window_and_none():
    assert action_repeat_count([], 3) == 0
    assert action_repeat_count(["a", "a", "b"], 3) == 2
    # window only sees the last 2
    assert action_repeat_count(["a", "a", "b"], 2) == 1
    # None entries are ignored
    assert action_repeat_count([None, None, "a"], 3) == 1
    assert action_repeat_count([None, None], 3) == 0
    assert action_repeat_count(["a", "a"], 0) == 0


def test_budget_predicates():
    assert over_step_budget(5, 5) and over_step_budget(6, 5)
    assert not over_step_budget(4, 5)
    assert not over_step_budget(100, 0)  # unbounded
    assert over_time_budget(5.0, 5.0)
    assert not over_time_budget(4.9, 5.0)
    assert not over_time_budget(1e9, 0)  # unbounded


def test_veto_streak():
    assert veto_streak([]) == 0
    assert veto_streak([False, False]) == 2
    assert veto_streak([False, True, False]) == 1
    assert veto_streak([True, True]) == 0
    assert veto_streak([False, None]) == 0  # None stops the run


def test_detector_step_budget():
    det = StuckDetector(StuckConfig(max_steps=3), time_fn=FakeClock())
    det.start()
    for _ in range(3):
        det.record("sig-unique-%d" % det.step_count, "k%d" % det.step_count, True)
    assert det.check() == StuckSignal.STEP_BUDGET


def test_detector_time_budget_precedes_no_progress():
    clk = FakeClock()
    det = StuckDetector(StuckConfig(max_seconds=10.0, no_change_limit=2, max_steps=0), time_fn=clk)
    det.start()
    det.record("same", "k0", True)
    det.record("same", "k1", True)  # would be NO_PROGRESS=2, but time wins if elapsed
    clk.advance(10.0)
    assert det.check() == StuckSignal.TIME_BUDGET


def test_detector_no_progress():
    det = StuckDetector(StuckConfig(no_change_limit=3, max_steps=0), time_fn=FakeClock())
    det.start()
    det.record("same", "k0", True)
    det.record("same", "k1", True)
    assert det.check() == StuckSignal.NONE
    det.record("same", "k2", True)
    assert det.check() == StuckSignal.NO_PROGRESS


def test_detector_action_repeat():
    det = StuckDetector(
        StuckConfig(action_dedup_window=3, action_dedup_limit=3, no_change_limit=99, max_steps=0),
        time_fn=FakeClock(),
    )
    det.start()
    for i in range(3):
        det.record("sig-%d" % i, "same-key", True)  # distinct frames, same action
    assert det.check() == StuckSignal.ACTION_REPEAT


def test_detector_veto_streak():
    det = StuckDetector(
        StuckConfig(veto_streak_limit=3, no_change_limit=99, action_dedup_limit=99, max_steps=0),
        time_fn=FakeClock(),
    )
    det.start()
    for i in range(3):
        det.record("sig-%d" % i, "key-%d" % i, False)  # distinct frames+actions, all veto
    assert det.check() == StuckSignal.VETO_STREAK
