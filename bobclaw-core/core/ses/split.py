"""BoBClaw Core — SES eval harness (§2.8): capability vs regression split.

Capability evals (low pass-rate, an IMPROVEMENT target) are scored SEPARATELY from regression
evals (near-100%, a PROTECTION target). Conflating them mis-prioritizes, so this module never
blends the two into a single number — ``SuiteScore`` keeps them distinct.

Pure & deterministic: no model calls, no I/O, no clock, no random, no global state.
"""
from __future__ import annotations

from typing import Callable, Iterable

from core.ses.types import (
    EvalCase,
    EvalKind,
    EvalResult,
    ScoreBreakdown,
    SesError,
    SuiteScore,
)


def score_results(results: Iterable[EvalResult]) -> SuiteScore:
    """Partition EvalResults STRICTLY by EvalKind into two independent ScoreBreakdowns.

    Each bucket computes: ``passed`` (# results with ``passed is True``), ``total`` (# in the
    bucket), ``pass_rate`` (passed/total, or 0.0 when total == 0), and ``failed_ids`` (a sorted
    tuple of ids whose ``passed`` is False). The two buckets are NEVER combined into one number.

    Raises SesError if any result's kind is not a valid EvalKind.
    """
    cap_passed = cap_total = 0
    reg_passed = reg_total = 0
    cap_failed_ids: list[str] = []
    reg_failed_ids: list[str] = []

    for result in results:
        if result.kind == EvalKind.CAPABILITY:
            cap_total += 1
            if result.passed:
                cap_passed += 1
            else:
                cap_failed_ids.append(result.id)
        elif result.kind == EvalKind.REGRESSION:
            reg_total += 1
            if result.passed:
                reg_passed += 1
            else:
                reg_failed_ids.append(result.id)
        else:
            raise SesError(f"Invalid EvalKind: {result.kind!r} for result {result.id!r}")

    return SuiteScore(
        capability=ScoreBreakdown(
            kind=EvalKind.CAPABILITY,
            passed=cap_passed,
            total=cap_total,
            pass_rate=(cap_passed / cap_total) if cap_total else 0.0,
            failed_ids=tuple(sorted(cap_failed_ids)),
        ),
        regression=ScoreBreakdown(
            kind=EvalKind.REGRESSION,
            passed=reg_passed,
            total=reg_total,
            pass_rate=(reg_passed / reg_total) if reg_total else 0.0,
            failed_ids=tuple(sorted(reg_failed_ids)),
        ),
    )


def run_evals(cases: Iterable[EvalCase], evaluator: Callable[[EvalCase], bool]) -> SuiteScore:
    """Run *evaluator* over each case and aggregate via :func:`score_results`.

    For each case an ``EvalResult(id=case.id, kind=case.kind, passed=bool(evaluator(case)))`` is
    built (the evaluator is handed the WHOLE case so it may read ``case.payload``; it returns
    pass/fail only), then delegated to ``score_results``. A bogus kind surfaces as SesError.
    """
    results = [
        EvalResult(id=case.id, kind=case.kind, passed=bool(evaluator(case)))
        for case in cases
    ]
    return score_results(results)
