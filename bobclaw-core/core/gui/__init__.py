"""BoBClaw GUI computer-use lane — v1 step-1 deterministic skeleton.

The capture→ground→act→verify loop + deterministic stuck detector, PURE and no-model,
with all real screen/click I/O behind :class:`~core.gui.surface.Surface` and all model
calls behind :class:`~core.gui.loop.Grounder`. This is the deterministic floor of the
unified architecture spec's §4 GUI lane; see ``tasks/2026-06-29-gui-cu-v1/DESIGN.md``.
"""
from __future__ import annotations

from core.gui.types import (
    A11yNode,
    Action,
    ActionKind,
    ActionResult,
    FailureType,
    Frame,
    FrameDiff,
    Postcondition,
    RunResult,
    RunStatus,
    StepRecord,
    StuckConfig,
    StuckSignal,
    Subgoal,
    Verdict,
)
from core.gui.framediff import (
    a11y_contains,
    a11y_index,
    frame_diff,
    frame_signature,
    hash_bytes,
)
from core.gui.actions import format_action, parse_action, validate_action
from core.gui.verify import verify_postcondition
from core.gui.classify import classify_failure
from core.gui.stuck import (
    StuckDetector,
    action_repeat_count,
    frame_repeat_count,
    over_step_budget,
    over_time_budget,
    veto_streak,
)
from core.gui.surface import FakeSurface, Surface
from core.gui.loop import GuiLoop, Grounder, ScriptedGrounder

__all__ = [
    # types
    "A11yNode", "Action", "ActionKind", "ActionResult", "FailureType", "Frame",
    "FrameDiff", "Postcondition", "RunResult", "RunStatus", "StepRecord",
    "StuckConfig", "StuckSignal", "Subgoal", "Verdict",
    # framediff
    "a11y_contains", "a11y_index", "frame_diff", "frame_signature", "hash_bytes",
    # actions
    "format_action", "parse_action", "validate_action",
    # verify / classify
    "verify_postcondition", "classify_failure",
    # stuck
    "StuckDetector", "action_repeat_count", "frame_repeat_count",
    "over_step_budget", "over_time_budget", "veto_streak",
    # surface / loop
    "FakeSurface", "Surface", "GuiLoop", "Grounder", "ScriptedGrounder",
]
