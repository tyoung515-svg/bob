"""BoBClaw Core — SES eval harness (§2.8): shared types (manager-authored contract spine).

The single source of truth every ``core.ses.*`` module imports from. PURE: enums and
dataclasses only — no I/O, no model calls, no clock, no random, no global mutable state.
All enums subclass ``str`` so values are JSON-serializable verbatim
(``EvalKind.CAPABILITY`` serializes as ``"capability"``).

Grounded in the unified architecture spec §2.8 (eval harness / SES): separate capability
evals (low pass-rate, improvement target) from regression evals (near-100%, protection
target); behavioral-fingerprint / trace-replay against captured ledger traces; a
``false_pass_rate`` measurement consumed by the §2.6 verification spine (MS-2 / MS-3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SesError(RuntimeError):
    """SES harness error: an invalid eval kind / label, or a malformed case/item."""


class EvalKind(str, Enum):
    """Which suite an eval belongs to. The two are scored SEPARATELY and never blended."""

    CAPABILITY = "capability"
    """Low pass-rate expected; an IMPROVEMENT target (reported, never gated to 100%)."""

    REGRESSION = "regression"
    """Near-100% expected; a PROTECTION target (a drop here is a real regression)."""


class Label(str, Enum):
    """Ground-truth label for a planted item scored by ``false_pass_rate``."""

    TRUE = "true"
    """The claim/action is correct — a good verifier SHOULD pass it."""

    WRONG = "wrong"
    """Planted-wrong — a good verifier SHOULD reject it."""


@dataclass(frozen=True)
class EvalCase:
    """One eval. ``kind`` is the suite bucket (capability|regression), NOT the ground truth.
    ``payload`` is opaque to the harness and handed to the evaluator."""

    id: str
    kind: EvalKind
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EvalResult:
    """The outcome of running one eval case."""

    id: str
    kind: EvalKind
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ScoreBreakdown:
    """A single suite's score. ``pass_rate`` is ``passed/total`` (0.0 when ``total == 0``)."""

    kind: EvalKind
    passed: int
    total: int
    pass_rate: float
    failed_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuiteScore:
    """The capability and regression scores, kept DELIBERATELY DISTINCT — there is no single
    blended number, because conflating an improvement target with a protection target
    mis-prioritizes (§2.8)."""

    capability: ScoreBreakdown
    regression: ScoreBreakdown

    def regression_ok(self, threshold: float = 1.0) -> bool:
        """Regression is a protection target: True iff ``regression.pass_rate >= threshold``
        (default 1.0 = near/at-100%). Capability is intentionally NOT gated this way.

        An EMPTY regression bucket is vacuously ok (no regression case failed), so this returns
        True when ``regression.total == 0`` — a suite with only capability cases must not raise a
        false regression alarm."""
        if self.regression.total == 0:
            return True
        return self.regression.pass_rate >= threshold


@dataclass(frozen=True)
class LabeledItem:
    """A planted, ground-truth-labelled item for ``false_pass_rate``. The verifier-under-test
    is handed ONLY ``payload`` (never ``label``) so it cannot peek at ground truth."""

    id: str
    payload: object
    label: Label

    @classmethod
    def from_obj(cls, obj: "LabeledItem | dict") -> "LabeledItem":
        """Coerce a LabeledItem (returned as-is) or a dict into a LabeledItem.

        A dict must carry ``id`` and ``payload``; the label comes from ``label`` (a Label or
        the string ``"true"`` / ``"wrong"``) or, as a fallback, a boolean ``is_true``. A
        missing / unrecognised label raises ``SesError``.
        """
        if isinstance(obj, cls):
            return obj
        if not isinstance(obj, dict):
            raise SesError(f"cannot coerce {type(obj).__name__} to LabeledItem")
        if "id" not in obj or "payload" not in obj:
            raise SesError(f"labeled item dict missing id/payload: {obj!r}")
        raw_label = obj.get("label")
        if raw_label is None and "is_true" in obj:
            raw_label = Label.TRUE if obj["is_true"] else Label.WRONG
        try:
            label = Label(raw_label)
        except (ValueError, TypeError) as exc:
            raise SesError(f"invalid label {raw_label!r} (expected 'true'/'wrong')") from exc
        return cls(id=str(obj["id"]), payload=obj["payload"], label=label)
