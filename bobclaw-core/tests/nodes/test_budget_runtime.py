import pytest
import math
import importlib

from core.nodes.budget_runtime import (
    approx_tokens,
    measure_spend,
    budget_config,
    plan_reservations,
    branch_spend_result,
    reconcile_branches,
)
from core.ledger.types import OVERSPEND_TRIGGER


class TestApproxTokens:
    def test_empty_string(self):
        assert approx_tokens("") == 0

    def test_known_string(self):
        # "hello" length 5, ceil(5/4)=2
        assert approx_tokens("hello") == 2

    def test_single_char(self):
        assert approx_tokens("a") == 1

    def test_multiline(self):
        text = "abc\ndef"
        # length 7, ceil(7/4)=2
        assert approx_tokens(text) == 2


class TestMeasureSpend:
    def test_with_total_tokens(self):
        messages = [{"content": "hello"}]
        response = "world"
        usage = {"total_tokens": 123}
        assert measure_spend(messages, response, usage) == 123

    def test_with_prompt_completion(self):
        messages = [{"content": "hello"}]
        response = "world"
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        assert measure_spend(messages, response, usage) == 15

    def test_without_usage_fallback(self):
        messages = [{"content": "hello"}]   # len 5 -> ceil(5/4)=2
        response = "world"                  # len 5 -> ceil(5/4)=2
        assert measure_spend(messages, response, usage=None) == 4

    def test_empty_inputs(self):
        assert measure_spend([], "", usage=None) == 0
        assert measure_spend([], "", usage={}) == 0

    def test_purity(self):
        # same inputs produce same result
        messages = [{"content": "abc"}]
        response = "xy"
        r1 = measure_spend(messages, response, usage={"total_tokens": 7})
        r2 = measure_spend(messages, response, usage={"total_tokens": 7})
        assert r1 == r2 == 7


class TestBudgetConfig:
    def test_none(self):
        assert budget_config(None) is None

    def test_empty_dict(self):
        assert budget_config({}) is None

    def test_no_pool(self):
        assert budget_config({"trigger": 2.0}) is None

    def test_defaults(self):
        result = budget_config({"pool": 1000})
        assert result == {
            "pool": 1000,
            "per_branch": None,
            "run_total": 0,
            "run_ceiling": None,
            "trigger": OVERSPEND_TRIGGER,  # 1.5
        }

    def test_explicit_trigger(self):
        result = budget_config({"pool": 500, "trigger": 2.0})
        assert result["trigger"] == 2.0

    def test_explicit_run_ceiling_and_run_total(self):
        result = budget_config({"pool": 500, "run_ceiling": 1000, "run_total": 50})
        assert result["run_ceiling"] == 1000
        assert result["run_total"] == 50

    def test_non_numeric_trigger_defaults(self):
        result = budget_config({"pool": 500, "trigger": "high"})
        assert result["trigger"] == OVERSPEND_TRIGGER

    def test_negative_pool_is_budget_off(self):
        # audit r1: a negative pool is meaningless -> budget OFF (None), not zero-reserve.
        assert budget_config({"pool": -100}) is None

    def test_negative_per_branch_falls_to_even_split(self):
        # audit r1: a negative per_branch normalizes to None (even split), never a negative request.
        result = budget_config({"pool": 100, "per_branch": -5})
        assert result["per_branch"] is None

    def test_bool_pool_rejected(self):
        # bool is an int subclass — True must NOT be accepted as a pool of 1.
        assert budget_config({"pool": True}) is None

    def test_zero_pool_is_valid(self):
        result = budget_config({"pool": 0})
        assert result is not None and result["pool"] == 0


class TestPlanReservations:
    def test_per_branch_given_exhaustion(self):
        # pool=10, per_branch=4, n=3 -> [4,4,0]
        reservations = plan_reservations(10, 3, 4)
        assert len(reservations) == 3
        for i, expected_res in enumerate([4, 4, 0]):
            r = reservations[i]
            assert r["idx"] == i
            assert r["requested"] == 4
            assert r["granted"] == (expected_res > 0)
            assert r["reservation"] == expected_res

    def test_even_split(self):
        # pool=12, n=3, per_branch=None -> 4 each
        reservations = plan_reservations(12, 3, per_branch=None)
        assert len(reservations) == 3
        for r in reservations:
            assert r["reservation"] == 4
            assert r["granted"] is True

    def test_n_zero(self):
        assert plan_reservations(100, 0, 10) == []

    def test_pool_exact_fit(self):
        # pool=8, per_branch=4, n=2 -> [4,4]
        reservations = plan_reservations(8, 2, 4)
        assert [r["reservation"] for r in reservations] == [4, 4]

    def test_pool_one_short(self):
        # pool=7, per_branch=4, n=2 -> [4,0] (since after first, pool=3<4)
        reservations = plan_reservations(7, 2, 4)
        assert [r["reservation"] for r in reservations] == [4, 0]

    def test_purity(self):
        r1 = plan_reservations(10, 3, 3)
        r2 = plan_reservations(10, 3, 3)
        assert r1 == r2


class TestBranchSpendResult:
    def test_under_reservation(self):
        result = branch_spend_result(10, 5)
        assert result["reservation"] == 10
        assert result["spent"] == 5
        assert result["tripped"] is False
        assert result["overspend_ratio"] == 0.5
        assert result["escalate"] is False
        assert result["reason"] is None

    def test_equal_reservation(self):
        result = branch_spend_result(10, 10)
        assert result["tripped"] is True
        assert result["overspend_ratio"] == 1.0
        assert result["escalate"] is False  # 1.0 < 1.5
        assert result["reason"] is None

    def test_overspend(self):
        # 2*reservation -> ratio=2.0 >= 1.5 -> escalate
        result = branch_spend_result(10, 20)
        assert result["tripped"] is True
        assert result["overspend_ratio"] == 2.0
        assert result["escalate"] is True
        assert result["reason"] == "OVERSPEND"

    def test_no_run_ceiling_in_branch(self):
        # branch_spend_result should never set reason to "RUN_CEILING"
        result = branch_spend_result(10, 20)
        assert result["reason"] == "OVERSPEND"
        # Even if we pass run arguments (they are ignored per contract)
        result_with_run = branch_spend_result(10, 20, trigger=1.5)
        assert result_with_run["reason"] == "OVERSPEND"

    def test_custom_trigger(self):
        # trigger 2.0, ratio 1.5 should NOT escalate
        result = branch_spend_result(10, 15, trigger=2.0)
        assert result["escalate"] is False
        assert result["reason"] is None

    def test_zero_reservation(self):
        result = branch_spend_result(0, 5)
        assert result["tripped"] is True
        assert result["overspend_ratio"] == 0.0  # as per spec
        # 0.0 < 1.5, so escalate False
        assert result["escalate"] is False
        assert result["reason"] is None


class TestReconcileBranches:
    def _build_branch(self, idx, reservation, spent, overspend_ratio, tripped, escalate, reason):
        return {
            "idx": idx,
            "reservation": reservation,
            "spent": spent,
            "overspend_ratio": overspend_ratio,
            "tripped": tripped,
            "escalate": escalate,
            "reason": reason,
        }

    def test_no_overspend_no_ceiling(self):
        # two branches, both under reservation
        branches = [
            self._build_branch(0, 30, 20, 20/30, False, False, None),
            self._build_branch(1, 30, 10, 10/30, False, False, None),
        ]
        result = reconcile_branches(100, branches, run_total_before=0, run_ceiling=None)
        expected_total_spent = 20 + 10  # 30
        expected_total_returned = (30-20) + (30-10)  # 30
        assert result["pool_before"] == 100
        assert result["total_reserved"] == 60
        assert result["total_spent"] == expected_total_spent
        assert result["total_returned"] == expected_total_returned
        assert result["pool_after"] == 100 - expected_total_spent  # 70
        assert result["run_total_before"] == 0
        assert result["run_total"] == 30
        assert result["interrupt"]["surfaced"] is False
        assert result["interrupt"]["reason"] is None
        assert result["interrupt"]["contested_branches"] == []
        assert result["interrupt"]["ceiling_hit"] is False

    def test_overspend_only(self):
        branches = [
            self._build_branch(0, 20, 30, 1.5, True, True, "OVERSPEND"),
            self._build_branch(1, 30, 10, 0.333, False, False, None),
        ]
        result = reconcile_branches(100, branches)
        expected_total_spent = 30 + 10  # 40
        expected_total_returned = 0 + (30-10)  # 20
        # Reserve-pool semantics: the pool's exposure is bounded by what was RESERVED —
        # an overspending branch can NOT drain the pool past its reservation (the point
        # of BIND-01). pool_after = pool - total_reserved + total_returned =
        # pool - sum(min(reservation, spent)) = 100 - (min(20,30)+min(30,10)) = 70.
        # This equals pool - total_spent ONLY when no branch overspends.
        assert result["total_reserved"] == 50
        assert result["total_spent"] == expected_total_spent
        assert result["total_returned"] == expected_total_returned
        assert result["pool_after"] == 100 - 50 + expected_total_returned  # 70
        assert result["pool_after"] == 100 - (min(20, 30) + min(30, 10))   # 70
        assert result["interrupt"]["surfaced"] is True
        assert result["interrupt"]["reason"] == "OVERSPEND"
        assert result["interrupt"]["contested_branches"] == [0]
        assert result["interrupt"]["ceiling_hit"] is False

    def test_run_ceiling_only(self):
        branches = [
            self._build_branch(0, 30, 10, 0.333, False, False, None),
            self._build_branch(1, 30, 20, 0.667, False, False, None),
        ]
        result = reconcile_branches(100, branches, run_total_before=50, run_ceiling=80)
        expected_total_spent = 10 + 20  # 30
        assert result["run_total"] == 80  # 50 + 30
        assert result["interrupt"]["surfaced"] is True
        assert result["interrupt"]["reason"] == "RUN_CEILING"
        assert result["interrupt"]["contested_branches"] == []
        assert result["interrupt"]["ceiling_hit"] is True

    def test_both_overspend_and_ceiling(self):
        branches = [
            self._build_branch(0, 20, 30, 1.5, True, True, "OVERSPEND"),
            self._build_branch(1, 30, 15, 0.5, False, False, None),
        ]
        result = reconcile_branches(100, branches, run_total_before=50, run_ceiling=95)
        # total_spent = 30+15=45, run_total = 50+45=95, exactly ceiling, should trigger ceiling
        assert result["interrupt"]["surfaced"] is True
        # order: overspend checked first, then ceiling -> "OVERSPEND+RUN_CEILING"
        assert result["interrupt"]["reason"] == "OVERSPEND+RUN_CEILING"
        assert result["interrupt"]["contested_branches"] == [0]
        assert result["interrupt"]["ceiling_hit"] is True

    def test_pool_consistency(self):
        # Reserve-pool semantics: pool_after = pool - total_reserved + total_returned
        # = pool - sum(min(reservation, spent)). Branch1 overspends (60 > 50), so it
        # debits the pool only its reservation (50), not its raw spend (60).
        branches = [
            self._build_branch(0, 50, 40, 0.8, False, False, None),
            self._build_branch(1, 50, 60, 1.2, True, False, None),  # overspend but ratio<1.5, not escalate
        ]
        result = reconcile_branches(100, branches)
        # Also check that total_returned is sum of per-branch reconcile_on_merge
        per_branch_returned = [
            max(0, 50-40),  # 10
            max(0, 50-60),  # 0
        ]
        assert result["total_returned"] == sum(per_branch_returned)  # 10
        assert result["pool_after"] == 100 - (min(50, 40) + min(50, 60))  # 100-90=10
        assert result["pool_after"] == 100 - 100 + sum(per_branch_returned)  # 10
        assert result["pool_after"] >= 0


class TestNoGlobalMutableState:
    def test_module_has_no_mutable_globals(self):
        import core.nodes.budget_runtime as mod
        for name in vars(mod):
            obj = getattr(mod, name)
            # skip private attributes that are not functions/classes/constants
            if name.startswith("_"):
                continue
            # allow int, float, str, bool, None, function, class, module
            assert not isinstance(obj, (list, dict, set)), (
                f"Module has mutable global: {name} = {type(obj)}"
            )
        # Also check that two calls with same args produce equal results
        r1 = approx_tokens("test")
        r2 = approx_tokens("test")
        assert r1 == r2

    def test_purity_of_all_functions(self):
        # Ensure each function is deterministic with same inputs
        args = ("hello " * 10,)
        a1 = approx_tokens(*args)
        a2 = approx_tokens(*args)
        assert a1 == a2

        m1 = measure_spend([{"content": "a"}], "b", usage=None)
        m2 = measure_spend([{"content": "a"}], "b", usage=None)
        assert m1 == m2

        b1 = budget_config({"pool": 100})
        b2 = budget_config({"pool": 100})
        assert b1 == b2

        p1 = plan_reservations(10, 3, 3)
        p2 = plan_reservations(10, 3, 3)
        assert p1 == p2

        br1 = branch_spend_result(10, 5)
        br2 = branch_spend_result(10, 5)
        assert br1 == br2

        # reconcile_branches with same branches
        branches = [{"idx": 0, "reservation": 20, "spent": 10, "tripped": False, "overspend_ratio": 0.5, "escalate": False, "reason": None}]
        r1 = reconcile_branches(100, branches)
        r2 = reconcile_branches(100, branches)
        assert r1 == r2
