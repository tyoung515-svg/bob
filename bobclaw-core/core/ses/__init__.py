"""BoBClaw Core — SES eval harness (§2.8).

A measurement substrate (NOT a generic test runner). Four load-bearing pieces:

- ``split``       — capability vs regression split (two distinct scores, never blended).
- ``fingerprint`` — trace-replay / behavioral fingerprint over a captured ledger trace,
                    invariant to wall-clock / commit-sha / uuid / ordering noise.
- ``falsepass``   — ``false_pass_rate``: the model-free measurement MS-2 / MS-3 call to
                    score their §2.6 verifiers against a planted-wrong set.
- ``recall``      — claim-extraction recall: the silent failure Default-FAIL cannot catch
                    (a missed claim never enters the gate and lands unverified).

Pure, stdlib-only, deterministic. No model calls, no I/O, no clock, no random.
"""
from __future__ import annotations

from core.ses.types import (  # noqa: F401
    EvalCase,
    EvalKind,
    EvalResult,
    Label,
    LabeledItem,
    ScoreBreakdown,
    SesError,
    SuiteScore,
)
from core.ses.split import run_evals, score_results  # noqa: F401
from core.ses.fingerprint import (  # noqa: F401
    DEFAULT_VOLATILE_KEYS,
    behavioral_fingerprint,
    extract_decisions,
    strip_volatile,
)
from core.ses.falsepass import false_pass_rate  # noqa: F401
from core.ses.recall import (  # noqa: F401
    DEFAULT_RECALL_THRESHOLD,
    RecallError,
    claim_key,
    extraction_recall,
    recall_eval_result,
)
