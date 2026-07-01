"""GUI computer-use v1 — step-1 shared contract (pure, stdlib-only).

Every other ``core.gui`` module codes against THESE frozen dataclasses + enums.
Nothing here does I/O, calls a model, or imports outside the stdlib — the whole
step-1 skeleton is the deterministic floor of the GUI lane (unified spec §4), with
all real screen/click I/O and all model calls behind injected seams
(:class:`~core.gui.surface.Surface`, :class:`~core.gui.loop.Grounder`).

See ``tasks/2026-06-29-gui-cu-v1/DESIGN.md`` for the behavioral spec and the map
of every deferred seam (durable git-DAG ledger §2.9, grounding model §4, cross-family
audit §6, manager/worker §8, SES §2.8) back to the unified architecture spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionKind(str, Enum):
    """The act primitives (unified §4 worker action space, GUI modality)."""

    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    KEY = "key"
    NOOP = "noop"


class FailureType(str, Enum):
    """Deterministic floor of the §4 Recovery taxonomy + the §9 ledger failure_type set.

    The model-driven failure *adjudication* is step-6; step-1 classifies deterministically.
    """

    NONE = "none"
    NO_STATE_CHANGE = "no_state_change"   # silent text-input / dead click — nothing moved
    WRONG_ELEMENT = "wrong_element"       # something changed but not the expected post-condition
    PARSE_ERROR = "parse_error"           # the action string didn't parse (malformed-syntax probe)
    AUDIT_VETO = "audit_veto"             # reserved for the step-4 cross-family critic
    PERCEPTION = "perception"
    GROUNDING_AMBIGUITY = "grounding_ambiguity"  # the grounder produced no action
    MODAL_INTERRUPT = "modal_interrupt"
    AUTH_BLOCK = "auth_block"
    LOADING = "loading"
    IMPOSSIBLE = "impossible"


class StuckSignal(str, Enum):
    """The deterministic stuck-detector trip signals (unified §7 detection)."""

    NONE = "none"
    NO_PROGRESS = "no_progress"     # frame signature unchanged N steps running
    ACTION_REPEAT = "action_repeat"  # same action repeated within the dedup window
    STEP_BUDGET = "step_budget"
    TIME_BUDGET = "time_budget"
    VETO_STREAK = "veto_streak"     # N consecutive failed verdicts (the §7←§6 wiring)


class RunStatus(str, Enum):
    COMPLETED = "completed"
    STUCK = "stuck"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class A11yNode:
    """One accessibility-tree node — the structured-first grounding signal (§3.1)."""

    role: str
    name: str = ""
    value: str = ""
    node_id: str = ""
    bounds: tuple[int, int, int, int] | None = None  # (x, y, w, h)


@dataclass(frozen=True, slots=True)
class Frame:
    """A captured surface state: a pixel HASH (never raw pixels in the pure core) + a11y.

    The pure core never holds image bytes — the :class:`~core.gui.surface.Surface`
    adapter hashes them into ``image_hash``; the frame-diff works on the hash + a11y.
    """

    seq: int
    size: tuple[int, int]
    image_hash: str
    a11y: tuple[A11yNode, ...] = ()


@dataclass(frozen=True, slots=True)
class Action:
    """A single act request. ``coord`` is a pixel fallback when a11y grounding is absent."""

    kind: ActionKind
    target: str = ""
    text: str = ""
    key: str = ""
    coord: tuple[int, int] | None = None
    direction: str = ""
    amount: int = 0


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome of ``Surface.act``. ``performed`` = the action was physically actuated,
    NOT that it semantically succeeded — silent-failure is performed=True with no state
    change, caught downstream by frame-diff + verify, never by the surface itself."""

    performed: bool
    error: str = ""


@dataclass(frozen=True, slots=True)
class FrameDiff:
    """The cheap "did anything happen" ground-truth between two frames (§3)."""

    changed: bool
    pixel_changed: bool
    a11y_changed: bool
    added: tuple[str, ...] = ()      # a11y node keys that appeared
    removed: tuple[str, ...] = ()    # a11y node keys that vanished
    text_changed: bool = False       # a shared node's value changed


@dataclass(frozen=True, slots=True)
class Postcondition:
    """The declared expected effect of a subgoal — the §2.6 semantic post-condition.

    Verified deterministically (Default-FAIL) against the post-action frame; the
    actor *declares* intent, the verifier confirms it independently.
    """

    expect_changed: bool = True
    present: tuple[str, ...] = ()                 # node_ids/names that must appear
    absent: tuple[str, ...] = ()                  # node_ids/names that must be gone
    text_in: tuple[tuple[str, str], ...] = ()     # (node_id-or-name, required substring)


@dataclass(frozen=True, slots=True)
class Verdict:
    """A Default-FAIL verification result: ``ok`` is False unless every (non-empty) criterion holds."""

    ok: bool
    reason: str = ""
    criteria: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One loop step's record. ``(subgoal, action, failure, flag)`` IS the §9/§2.1
    structured failure-note schema — survives compaction, becomes the SES label."""

    idx: int
    subgoal: str
    action: Action | None
    pre_hash: str
    post_hash: str
    diff: FrameDiff | None
    verdict: Verdict | None
    failure: FailureType
    flag: str = ""   # "" | "dont-retry" | "try-alt"


@dataclass(frozen=True, slots=True)
class StuckConfig:
    """Thresholds for the deterministic stuck detector (unified §13 knobs)."""

    no_change_limit: int = 3
    action_dedup_window: int = 3
    action_dedup_limit: int = 3
    max_steps: int = 50
    max_seconds: float = 300.0
    veto_streak_limit: int = 3


@dataclass(frozen=True, slots=True)
class Subgoal:
    text: str
    postcondition: Postcondition = field(default_factory=Postcondition)


@dataclass(frozen=True, slots=True)
class RunResult:
    status: RunStatus
    steps: tuple[StepRecord, ...]
    stuck_signal: StuckSignal
    completed: int
    total: int
