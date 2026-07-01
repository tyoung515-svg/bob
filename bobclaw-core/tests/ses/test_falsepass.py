from __future__ import annotations

import pytest

from core.ses.types import LabeledItem, Label, SesError
from core.ses.falsepass import false_pass_rate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_true_item(item_id: str, payload: object = None) -> LabeledItem:
    return LabeledItem(
        id=item_id,
        payload=payload if payload is not None else {},
        label=Label.TRUE,
    )


def _make_wrong_item(item_id: str, payload: object = None) -> LabeledItem:
    return LabeledItem(
        id=item_id,
        payload=payload if payload is not None else {},
        label=Label.WRONG,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFalsePassRateBlind:
    """A blind verifier (always returns True)."""

    def test_blind_verifier(self):
        true_items = [
            _make_true_item("t1"),
            _make_true_item("t2"),
        ]
        wrong_items = [
            _make_wrong_item("w1"),
            _make_wrong_item("w2"),
            _make_wrong_item("w3"),
        ]
        items = true_items + wrong_items
        result = false_pass_rate(items, lambda p: True)

        assert result["false_pass_rate"] == pytest.approx(1.0)
        assert result["false_fail_rate"] == pytest.approx(0.0)
        assert result["n_total"] == 5
        assert result["n_wrong"] == 3
        assert result["n_true"] == 2
        assert result["wrong_passed"] == 3
        assert result["wrong_caught"] == 0
        assert result["true_passed"] == 2
        assert result["true_failed"] == 0
        assert result["false_pass_ids"] == ["w1", "w2", "w3"]
        assert result["false_fail_ids"] == []

    def test_false_pass_ids_are_sorted_from_unordered_input(self):
        # Feed wrong items OUT of order so an impl that omits sorted() would be caught.
        wrong_items = [
            _make_wrong_item("w3"),
            _make_wrong_item("w1"),
            _make_wrong_item("w2"),
        ]
        result = false_pass_rate(wrong_items, lambda p: True)
        assert result["false_pass_ids"] == ["w1", "w2", "w3"]

    def test_false_fail_ids_are_sorted_from_unordered_input(self):
        # Symmetric to false_pass_ids: feed true items OUT of order, verifier rejects all ->
        # false_fail_ids must be sorted (catches an impl that omits sorting on this branch).
        true_items = [
            _make_true_item("t3"),
            _make_true_item("t1"),
            _make_true_item("t2"),
        ]
        result = false_pass_rate(true_items, lambda p: False)
        assert result["false_fail_ids"] == ["t1", "t2", "t3"]


class TestFalsePassRatePerfect:
    """A perfect verifier that reads an engineered signal in the payload."""

    def test_perfect_verifier(self):
        true_items = [
            _make_true_item("t1", {"number": 5, "source_value": 5}),
            _make_true_item("t2", {"number": 0, "source_value": 0}),
        ]
        wrong_items = [
            _make_wrong_item("w1", {"number": 5, "source_value": 7}),
            _make_wrong_item("w2", {"number": 0, "source_value": 1}),
            _make_wrong_item("w3", {"number": 10, "source_value": 9}),
        ]
        items = true_items + wrong_items
        verifier = lambda p: p["number"] == p["source_value"]
        result = false_pass_rate(items, verifier)

        assert result["false_pass_rate"] == pytest.approx(0.0)
        assert result["false_fail_rate"] == pytest.approx(0.0)
        assert result["n_wrong"] == 3
        assert result["wrong_passed"] == 0
        assert result["wrong_caught"] == 3
        assert result["true_passed"] == 2
        assert result["true_failed"] == 0
        assert result["false_pass_ids"] == []
        assert result["false_fail_ids"] == []


class TestFalsePassRatePartial:
    """A partial verifier that passes 1 out of 3 wrong items."""

    def test_partial_verifier_exact_fraction(self):
        # true items all pass
        true_items = [
            _make_true_item("t1", {"pass": True}),
            _make_true_item("t2", {"pass": True}),
        ]
        # wrong items: only one passes
        wrong_items = [
            _make_wrong_item("w_pass", {"pass": True}),   # this one will slip through
            _make_wrong_item("w_fail1", {"pass": False}),
            _make_wrong_item("w_fail2", {"pass": False}),
        ]
        items = true_items + wrong_items
        verifier = lambda p: p["pass"]
        result = false_pass_rate(items, verifier)

        assert result["false_pass_rate"] == pytest.approx(1.0 / 3.0)
        assert result["false_fail_rate"] == pytest.approx(0.0)
        assert result["n_wrong"] == 3
        assert result["wrong_passed"] == 1
        assert result["wrong_caught"] == 2
        assert result["true_passed"] == 2
        assert result["true_failed"] == 0
        assert result["false_pass_ids"] == ["w_pass"]
        assert result["false_fail_ids"] == []


class TestVerifierOnlyReceivesPayload:
    """Verify the verifier never sees the label."""

    def test_verifier_never_sees_label(self):
        seen_args = []

        def capturing_verifier(payload):
            seen_args.append(payload)
            return True

        items = [
            _make_true_item("t1", {"a": 1}),
            _make_wrong_item("w1", {"b": 2}),
        ]
        false_pass_rate(items, capturing_verifier)

        for payload in seen_args:
            assert "label" not in payload, "Verifier should not have access to 'label' key"


class TestEdgeCases:
    """n_wrong == 0 and empty input."""

    def test_n_wrong_zero(self):
        items = [
            _make_true_item("t1"),
            _make_true_item("t2"),
        ]
        result = false_pass_rate(items, lambda p: True)
        assert result["false_pass_rate"] == pytest.approx(0.0)
        assert result["false_fail_rate"] == pytest.approx(0.0)
        assert result["n_wrong"] == 0
        assert result["wrong_passed"] == 0
        assert result["wrong_caught"] == 0
        assert result["false_pass_ids"] == []

    def test_empty_input_all_zero(self):
        result = false_pass_rate([], lambda p: True)
        assert result == {
            "false_pass_rate": 0.0,
            "false_fail_rate": 0.0,
            "n_total": 0,
            "n_wrong": 0,
            "n_true": 0,
            "wrong_passed": 0,
            "wrong_caught": 0,
            "true_passed": 0,
            "true_failed": 0,
            "false_pass_ids": [],
            "false_fail_ids": [],
        }


class TestDictCoercion:
    """LabeledItem.from_obj converts dicts correctly."""

    def test_dict_with_label_string(self):
        items = [
            {"id": "d1", "payload": {"x": 1}, "label": "true"},
            {"id": "d2", "payload": {"x": 2}, "label": "wrong"},
        ]
        result = false_pass_rate(items, lambda p: p["x"] > 1)
        # d1 (true) has x=1 → fails → true_failed=1
        # d2 (wrong) has x=2 → passes → wrong_passed=1
        assert result["n_total"] == 2
        assert result["n_wrong"] == 1
        assert result["n_true"] == 1
        assert result["true_failed"] == 1
        assert result["wrong_passed"] == 1
        assert result["false_pass_rate"] == pytest.approx(1.0)
        assert result["false_fail_rate"] == pytest.approx(1.0)

    def test_dict_with_is_true(self):
        items = [
            {"id": "d1", "payload": {}, "is_true": True},
            {"id": "d2", "payload": {}, "is_true": False},
        ]
        result = false_pass_rate(items, lambda p: True)
        assert result["n_true"] == 1
        assert result["n_wrong"] == 1
        assert result["true_passed"] == 1
        assert result["wrong_passed"] == 1


class TestBadLabel:
    """A bad label raises SesError."""

    def test_invalid_label_string_raises(self):
        with pytest.raises(SesError):
            LabeledItem.from_obj({"id": "x", "payload": {}, "label": "invalid"})

    def test_missing_label_raises(self):
        with pytest.raises(SesError):
            LabeledItem.from_obj({"id": "x", "payload": {}})

    def test_none_label_raises(self):
        with pytest.raises(SesError):
            LabeledItem.from_obj({"id": "x", "payload": {}, "label": None})


class TestRatesAndCoercion:
    """Denominator guards, non-bool coercion, and bad-input coercion."""

    def test_false_fail_rate_zero_when_no_true(self):
        # Only wrong items -> n_true == 0 -> false_fail_rate 0.0 (no ZeroDivisionError).
        items = [_make_wrong_item("w1", {"pass": False}), _make_wrong_item("w2", {"pass": True})]
        result = false_pass_rate(items, lambda p: p["pass"])
        assert result["n_true"] == 0
        assert result["false_fail_rate"] == pytest.approx(0.0)
        assert result["false_pass_rate"] == pytest.approx(0.5)  # w2 slipped through

    def test_false_fail_rate_positive_exact(self):
        # 1 of 2 true items wrongly rejected -> false_fail_rate 0.5 (guards an always-0.0 bug).
        items = [
            _make_true_item("t_ok", {"pass": True}),
            _make_true_item("t_bad", {"pass": False}),
        ]
        result = false_pass_rate(items, lambda p: p["pass"])
        assert result["false_fail_rate"] == pytest.approx(0.5)
        assert result["true_failed"] == 1
        assert result["false_fail_ids"] == ["t_bad"]

    def test_non_bool_verifier_return_is_coerced(self):
        # A verifier returning a truthy non-bool (1) and a falsy non-bool (None / 0) is bool()-coerced.
        items = [
            _make_wrong_item("w_truthy", {"v": 1}),     # 1 -> True  -> false pass
            _make_wrong_item("w_falsy", {"v": 0}),      # 0 -> False -> caught
            _make_true_item("t_none", {"v": None}),     # None -> False -> false fail
        ]
        result = false_pass_rate(items, lambda p: p["v"])
        assert result["wrong_passed"] == 1
        assert result["wrong_caught"] == 1
        assert result["true_failed"] == 1
        assert result["false_pass_ids"] == ["w_truthy"]
        assert result["false_fail_ids"] == ["t_none"]

    def test_non_dict_non_item_input_raises(self):
        with pytest.raises(SesError):
            false_pass_rate([42], lambda p: True)
        with pytest.raises(SesError):
            false_pass_rate([["not", "a", "dict"]], lambda p: True)

    def test_dict_missing_id_or_payload_raises(self):
        with pytest.raises(SesError):
            false_pass_rate([{"payload": {}, "label": "true"}], lambda p: True)  # no id
        with pytest.raises(SesError):
            false_pass_rate([{"id": "x", "label": "true"}], lambda p: True)      # no payload


class TestDuplicateIds:
    """Duplicate ids are allowed; each item is counted separately."""

    def test_duplicate_wrong_ids(self):
        items = [
            _make_wrong_item("dup", {"pass": True}),
            _make_wrong_item("dup", {"pass": True}),
            _make_true_item("t1"),
        ]
        result = false_pass_rate(items, lambda p: p.get("pass", True))
        # both wrong items pass → false_pass_ids should contain "dup" twice
        assert result["n_wrong"] == 2
        assert result["wrong_passed"] == 2
        assert result["false_pass_ids"] == ["dup", "dup"]

    def test_duplicate_true_ids(self):
        items = [
            _make_true_item("dup", {"pass": False}),
            _make_true_item("dup", {"pass": False}),
            _make_wrong_item("w1"),
        ]
        result = false_pass_rate(items, lambda p: p.get("pass", True))
        # true items fail → false_fail_ids contains "dup" twice
        assert result["n_true"] == 2
        assert result["true_failed"] == 2
        assert result["false_fail_ids"] == ["dup", "dup"]
