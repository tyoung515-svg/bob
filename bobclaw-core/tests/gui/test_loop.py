"""Integration tests for core.gui.loop — the deterministic capture→ground→act→verify loop."""
from __future__ import annotations

from core.gui import (
    Action,
    ActionKind,
    FailureType,
    FakeSurface,
    GuiLoop,
    Postcondition,
    RunStatus,
    ScriptedGrounder,
    StuckConfig,
    StuckSignal,
    Subgoal,
    format_action,
)
from conftest import FakeClock, frame, node

CLICK_MENU = Action(ActionKind.CLICK, target="menu")
CLICK_SETTINGS = Action(ActionKind.CLICK, target="settings")
CLICK_GHOST = Action(ActionKind.CLICK, target="ghost")


def _app_surface():
    home = frame("home", node("button", "menu", node_id="menu"))
    menu = frame("menu", node("button", "settings", node_id="settings"))
    settings = frame("settings", node("group", "panel", node_id="panel"))
    return FakeSurface(
        states={"home": home, "menu": menu, "settings": settings},
        transitions={
            ("home", format_action(CLICK_MENU)): "menu",
            ("menu", format_action(CLICK_SETTINGS)): "settings",
        },
        start="home",
    )


def test_scripted_task_completes_with_verified_postconditions():
    surface = _app_surface()
    grounder = ScriptedGrounder({
        "open menu": CLICK_MENU,
        "open settings": "click(target=settings)",  # string form is parsed
    })
    plan = [
        Subgoal("open menu", Postcondition(present=("settings",))),
        Subgoal("open settings", Postcondition(present=("panel",))),
    ]
    result = GuiLoop(surface, grounder).run(plan)
    assert result.status == RunStatus.COMPLETED
    assert result.completed == 2 and result.total == 2
    assert result.stuck_signal == StuckSignal.NONE
    # every recorded step that completed a subgoal has failure NONE
    verified = [s for s in result.steps if s.verdict and s.verdict.ok]
    assert len(verified) == 2
    assert all(s.failure == FailureType.NONE for s in verified)


def test_grounding_ambiguity_fails_run():
    surface = _app_surface()
    grounder = ScriptedGrounder({})  # nothing mapped -> ground returns None
    result = GuiLoop(surface, grounder).run([Subgoal("do thing", Postcondition())])
    assert result.status == RunStatus.FAILED
    assert result.completed == 0
    assert result.steps[0].failure == FailureType.GROUNDING_AMBIGUITY
    assert result.steps[0].flag == "try-alt"
    assert result.steps[0].action is None


def test_no_progress_trips_stuck_detector():
    surface = _app_surface()
    grounder = ScriptedGrounder({"impossible": CLICK_GHOST})  # no transition for ghost
    cfg = StuckConfig(no_change_limit=3, max_steps=0, action_dedup_limit=99, veto_streak_limit=99)
    result = GuiLoop(surface, grounder, cfg=cfg).run(
        [Subgoal("impossible", Postcondition(present=("never",)))]
    )
    assert result.status == RunStatus.STUCK
    assert result.stuck_signal == StuckSignal.NO_PROGRESS
    assert result.completed == 0
    # the silent-failure was recorded as no-state-change failure notes
    assert all(s.failure == FailureType.NO_STATE_CHANGE for s in result.steps)


def test_veto_streak_trips_when_state_moves_but_never_verifies():
    # action changes the frame each step (so NO_PROGRESS never fires) but the
    # postcondition never holds -> consecutive vetoes -> VETO_STREAK.
    a = frame("a", node("x", node_id="a"))
    b = frame("b", node("y", node_id="b"))
    toggle = Action(ActionKind.CLICK, target="toggle")
    surface = FakeSurface(
        states={"a": a, "b": b},
        transitions={
            ("a", format_action(toggle)): "b",
            ("b", format_action(toggle)): "a",
        },
        start="a",
    )
    grounder = ScriptedGrounder({"loop": toggle})
    cfg = StuckConfig(no_change_limit=99, action_dedup_limit=99, veto_streak_limit=3, max_steps=0)
    result = GuiLoop(surface, grounder, cfg=cfg).run(
        [Subgoal("loop", Postcondition(present=("never",)))]
    )
    assert result.status == RunStatus.STUCK
    assert result.stuck_signal == StuckSignal.VETO_STREAK


def test_invalid_action_is_recorded_not_acted():
    # audit fix coverage: grounder returns an Action that fails validate_action
    surface = _app_surface()
    grounder = ScriptedGrounder({"bad": Action(ActionKind.CLICK)})  # no target/coord -> invalid
    result = GuiLoop(surface, grounder).run([Subgoal("bad", Postcondition(present=("x",)))])
    assert result.status == RunStatus.STUCK  # never verifies -> NO_PROGRESS
    first = result.steps[0]
    assert first.action is not None
    assert first.failure == FailureType.WRONG_ELEMENT
    assert first.verdict is not None and not first.verdict.ok
    assert first.diff is None  # invalid action is never actuated


def test_absolute_ceiling_guarantees_termination(monkeypatch):
    # audit fix: even with EVERY stuck limit effectively disabled, the loop must terminate.
    import core.gui.loop as loop_mod
    monkeypatch.setattr(loop_mod, "_ABSOLUTE_MAX_STEPS", 50)
    a = frame("a", node("x", node_id="a"))
    b = frame("b", node("y", node_id="b"))
    toggle = Action(ActionKind.CLICK, target="toggle")
    surface = FakeSurface(
        states={"a": a, "b": b},
        transitions={
            ("a", format_action(toggle)): "b",
            ("b", format_action(toggle)): "a",
        },
        start="a",
    )
    grounder = ScriptedGrounder({"x": toggle})
    cfg = StuckConfig(max_steps=0, max_seconds=0, no_change_limit=999,
                      action_dedup_limit=999, veto_streak_limit=999)
    result = GuiLoop(surface, grounder, cfg=cfg).run(
        [Subgoal("x", Postcondition(present=("never",)))]
    )
    assert result.status == RunStatus.STUCK
    assert len(result.steps) == 50


def test_step_budget_is_backstop():
    surface = _app_surface()
    grounder = ScriptedGrounder({"impossible": CLICK_GHOST})
    cfg = StuckConfig(no_change_limit=99, action_dedup_limit=99, veto_streak_limit=99, max_steps=5)
    result = GuiLoop(surface, grounder, cfg=cfg).run(
        [Subgoal("impossible", Postcondition(present=("never",)))]
    )
    assert result.status == RunStatus.STUCK
    assert result.stuck_signal == StuckSignal.STEP_BUDGET
    assert len(result.steps) == 5
