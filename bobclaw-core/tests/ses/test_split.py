from __future__ import annotations

import pytest

from core.ses.split import run_evals, score_results
from core.ses.types import EvalKind, EvalCase, EvalResult, ScoreBreakdown, SuiteScore, SesError


def make_eval_result(id: str, kind: EvalKind, passed: bool, detail: str = "") -> EvalResult:
    return EvalResult(id=id, kind=kind, passed=passed, detail=detail)


def make_eval_case(id: str, kind: EvalKind, payload: dict | None = None) -> EvalCase:
    return EvalCase(id=id, kind=kind, payload=payload or {})


class TestScoreResults:
    def test_partition_into_two_distinct_scores(self) -> None:
        results = [
            make_eval_result("cap_pass_1", EvalKind.CAPABILITY, True),
            make_eval_result("cap_pass_2", EvalKind.CAPABILITY, True),
            make_eval_result("cap_fail_1", EvalKind.CAPABILITY, False),
            make_eval_result("cap_fail_2", EvalKind.CAPABILITY, False),
            make_eval_result("reg_pass_1", EvalKind.REGRESSION, True),
            make_eval_result("reg_pass_2", EvalKind.REGRESSION, True),
            make_eval_result("reg_pass_3", EvalKind.REGRESSION, True),
            make_eval_result("reg_fail_1", EvalKind.REGRESSION, False),
        ]
        suite = score_results(results)

        # Capability: 2 passed, 4 total -> 0.5, failed_ids sorted: cap_fail_1, cap_fail_2
        assert suite.capability.kind == EvalKind.CAPABILITY
        assert suite.capability.passed == 2
        assert suite.capability.total == 4
        assert suite.capability.pass_rate == 0.5
        assert suite.capability.failed_ids == ("cap_fail_1", "cap_fail_2")

        # Regression: 3 passed, 4 total -> 0.75, failed_ids: reg_fail_1
        assert suite.regression.kind == EvalKind.REGRESSION
        assert suite.regression.passed == 3
        assert suite.regression.total == 4
        assert suite.regression.pass_rate == 0.75
        assert suite.regression.failed_ids == ("reg_fail_1",)

    def test_independent_pass_rates_not_equal(self) -> None:
        results = [
            make_eval_result("c1", EvalKind.CAPABILITY, True),
            make_eval_result("c2", EvalKind.CAPABILITY, False),
            make_eval_result("c3", EvalKind.CAPABILITY, False),
            make_eval_result("r1", EvalKind.REGRESSION, True),
            make_eval_result("r2", EvalKind.REGRESSION, True),
            make_eval_result("r3", EvalKind.REGRESSION, True),
        ]
        suite = score_results(results)
        # Cap: 1/3 ≈ 0.333, Reg: 3/3 = 1.0 — the two are scored independently and differ.
        assert suite.capability.pass_rate != suite.regression.pass_rate
        assert suite.capability.pass_rate == pytest.approx(1 / 3)
        assert suite.regression.pass_rate == 1.0

    def test_empty_bucket_yields_zero_breakdowns(self) -> None:
        # Only regression results – capability bucket empty
        results = [
            make_eval_result("r1", EvalKind.REGRESSION, True),
            make_eval_result("r2", EvalKind.REGRESSION, False),
        ]
        suite = score_results(results)
        # Capability empty
        assert suite.capability.kind == EvalKind.CAPABILITY
        assert suite.capability.passed == 0
        assert suite.capability.total == 0
        assert suite.capability.pass_rate == 0.0
        assert suite.capability.failed_ids == ()
        # Regression normal
        assert suite.regression.total == 2
        assert suite.regression.passed == 1

        # Only capability results – regression empty
        results2 = [
            make_eval_result("c1", EvalKind.CAPABILITY, False),
        ]
        suite2 = score_results(results2)
        assert suite2.regression.kind == EvalKind.REGRESSION
        assert suite2.regression.passed == 0
        assert suite2.regression.total == 0
        assert suite2.regression.pass_rate == 0.0
        assert suite2.regression.failed_ids == ()

    def test_regression_ok_true_when_all_pass(self) -> None:
        results = [
            make_eval_result("r1", EvalKind.REGRESSION, True),
            make_eval_result("r2", EvalKind.REGRESSION, True),
        ]
        suite = score_results(results)
        assert suite.regression_ok() is True
        assert suite.regression_ok(threshold=0.5) is True

    def test_regression_ok_false_when_any_fails(self) -> None:
        results = [
            make_eval_result("r1", EvalKind.REGRESSION, True),
            make_eval_result("r2", EvalKind.REGRESSION, False),
        ]
        suite = score_results(results)
        assert suite.regression_ok() is False
        assert suite.regression_ok(threshold=0.5) is True   # 0.5 still ok
        assert suite.regression_ok(threshold=1.0) is False

    def test_failed_ids_sorted(self) -> None:
        results = [
            make_eval_result("z_last", EvalKind.CAPABILITY, False),
            make_eval_result("a_first", EvalKind.CAPABILITY, False),
            make_eval_result("m_mid", EvalKind.CAPABILITY, False),
        ]
        suite = score_results(results)
        assert suite.capability.failed_ids == ("a_first", "m_mid", "z_last")

    def test_bogus_kind_raises_ses_error(self) -> None:
        # EvalResult is a plain frozen dataclass (no runtime kind validation), so a bogus string
        # kind can be constructed; score_results must reject it (str-enum equality won't match).
        bogus = EvalResult(id="bad", kind="invalid_kind", passed=True)
        with pytest.raises(SesError, match="Invalid EvalKind"):
            score_results([bogus])

    def test_bogus_kind_in_run_evals_raises(self) -> None:
        # run_evals delegates to score_results, so a bogus-kind case must surface SesError too.
        bogus_case = EvalCase(id="bad", kind="invalid_kind", payload={})
        with pytest.raises(SesError, match="Invalid EvalKind"):
            run_evals([bogus_case], lambda c: True)

    def test_run_evals_uses_evaluator_and_delegates(self) -> None:
        # Create a payload-driven evaluator
        def evaluator(case: EvalCase) -> bool:
            return case.payload.get("pass", False)

        cases = [
            make_eval_case("c1", EvalKind.CAPABILITY, {"pass": True}),
            make_eval_case("c2", EvalKind.CAPABILITY, {"pass": False}),
            make_eval_case("c3", EvalKind.CAPABILITY, {"pass": True}),
            make_eval_case("r1", EvalKind.REGRESSION, {"pass": True}),
            make_eval_case("r2", EvalKind.REGRESSION, {"pass": False}),
        ]
        suite = run_evals(cases, evaluator)
        # Manually compute expected
        expected_results = [
            EvalResult("c1", EvalKind.CAPABILITY, True),
            EvalResult("c2", EvalKind.CAPABILITY, False),
            EvalResult("c3", EvalKind.CAPABILITY, True),
            EvalResult("r1", EvalKind.REGRESSION, True),
            EvalResult("r2", EvalKind.REGRESSION, False),
        ]
        expected_suite = score_results(expected_results)
        assert suite.capability.passed == expected_suite.capability.passed
        assert suite.capability.total == expected_suite.capability.total
        assert suite.capability.pass_rate == expected_suite.capability.pass_rate
        assert suite.capability.failed_ids == expected_suite.capability.failed_ids
        assert suite.regression == expected_suite.regression


class TestRunEvals:
    def test_returns_suite_equivalent_to_manual_score_results(self) -> None:
        # Ensure run_evals produces identical SuiteScore as calling score_results on the produced results
        def dummy_evaluator(case: EvalCase) -> bool:
            return case.id.endswith("p")

        cases = [
            make_eval_case("cap_p", EvalKind.CAPABILITY),
            make_eval_case("cap_f", EvalKind.CAPABILITY),
            make_eval_case("reg_p", EvalKind.REGRESSION),
            make_eval_case("reg_f", EvalKind.REGRESSION),
        ]
        suite_from_run = run_evals(cases, dummy_evaluator)
        # Manually produce results:
        manual_results = [
            EvalResult("cap_p", EvalKind.CAPABILITY, True),
            EvalResult("cap_f", EvalKind.CAPABILITY, False),
            EvalResult("reg_p", EvalKind.REGRESSION, True),
            EvalResult("reg_f", EvalKind.REGRESSION, False),
        ]
        manual_suite = score_results(manual_results)
        assert suite_from_run == manual_suite

    def test_evaluator_receives_full_case(self) -> None:
        # Verify evaluator gets case with id and payload
        captured = []

        def spy_evaluator(case: EvalCase) -> bool:
            captured.append((case.id, case.kind, case.payload))
            return True

        cases = [
            make_eval_case("test", EvalKind.CAPABILITY, {"foo": "bar"}),
        ]
        run_evals(cases, spy_evaluator)
        assert len(captured) == 1
        capt_id, capt_kind, capt_payload = captured[0]
        assert capt_id == "test"
        assert capt_kind == EvalKind.CAPABILITY
        assert capt_payload == {"foo": "bar"}


class TestEmptyAndVacuous:
    def test_score_results_empty_list(self) -> None:
        # Both buckets empty, no divide-by-zero, no crash.
        suite = score_results([])
        assert suite.capability.total == 0 and suite.capability.pass_rate == 0.0
        assert suite.regression.total == 0 and suite.regression.pass_rate == 0.0
        assert suite.capability.failed_ids == () and suite.regression.failed_ids == ()

    def test_regression_ok_vacuous_on_empty_bucket(self) -> None:
        # An all-capability suite has NO regression cases -> regression_ok() must NOT false-alarm.
        suite = score_results([
            make_eval_result("c1", EvalKind.CAPABILITY, False),
            make_eval_result("c2", EvalKind.CAPABILITY, True),
        ])
        assert suite.regression.total == 0
        assert suite.regression_ok() is True
        # an empty whole suite is also vacuously ok
        assert score_results([]).regression_ok() is True
