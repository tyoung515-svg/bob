"""BoBClaw Core — SES eval harness (§2.8): false-pass measurement.

The model-free measurement MS-2 / MS-3 call to score their §2.6 verifiers against a planted
set of labelled claims/actions: how many planted-WRONG items did the verifier wrongly accept?

PURE MEASUREMENT — this never imports or calls a backend / network / model. The verifier
callable passed in is the only thing that may (that is the caller's concern). The harness hands
the verifier ONLY ``item.payload`` (never ``item.label``), so a verifier cannot peek at ground
truth. Deterministic: no clock, no random, no global state.
"""
from __future__ import annotations

from typing import Callable, Iterable

from core.ses.types import Label, LabeledItem, SesError


def false_pass_rate(
    labeled_items: Iterable["LabeledItem | dict"],
    verifier: Callable[[object], bool],
) -> dict:
    """Measure a verifier's false-pass rate on a labelled set.

    Args:
        labeled_items: iterable of ``LabeledItem`` or dict (coerced via ``LabeledItem.from_obj``).
        verifier: ``Callable[[payload], bool]`` — True iff the verifier ACCEPTED/passed the item.
            Receives ONLY ``item.payload``; a non-bool return is coerced via ``bool()``.

    Returns a breakdown dict (the stable contract MS-2/MS-3 depend on — do not rename keys)::

        {
          "false_pass_rate": float,   # wrong_passed / n_wrong  (PRIMARY; lower better; 0.0 if n_wrong==0)
          "false_fail_rate": float,   # true_failed  / n_true   (over-rejection; 0.0 if n_true==0)
          "n_total": int, "n_wrong": int, "n_true": int,
          "wrong_passed": int,        # FALSE PASSES (verifier accepted a planted-wrong item — BAD)
          "wrong_caught": int,
          "true_passed": int,
          "true_failed": int,         # FALSE FAILS (verifier rejected a true item)
          "false_pass_ids": list[str],  # sorted ids of wrong items that slipped through (surface these)
          "false_fail_ids": list[str],  # sorted ids of true items wrongly rejected
        }

    Raises SesError if an item cannot be coerced / carries a bad label.
    """
    n_total = n_wrong = n_true = 0
    wrong_passed = wrong_caught = true_passed = true_failed = 0
    false_pass_ids: list[str] = []
    false_fail_ids: list[str] = []

    for raw_item in labeled_items:
        item = LabeledItem.from_obj(raw_item)
        n_total += 1
        # The verifier must NEVER see the label — pass only the payload.
        passed = bool(verifier(item.payload))

        if item.label is Label.WRONG:
            n_wrong += 1
            if passed:
                wrong_passed += 1
                false_pass_ids.append(item.id)
            else:
                wrong_caught += 1
        elif item.label is Label.TRUE:
            n_true += 1
            if passed:
                true_passed += 1
            else:
                true_failed += 1
                false_fail_ids.append(item.id)
        else:  # coercion guarantees a valid label; guard defensively
            raise SesError(f"unexpected label {item.label!r}")

    return {
        "false_pass_rate": (wrong_passed / n_wrong) if n_wrong else 0.0,
        "false_fail_rate": (true_failed / n_true) if n_true else 0.0,
        "n_total": n_total,
        "n_wrong": n_wrong,
        "n_true": n_true,
        "wrong_passed": wrong_passed,
        "wrong_caught": wrong_caught,
        "true_passed": true_passed,
        "true_failed": true_failed,
        "false_pass_ids": sorted(false_pass_ids),
        "false_fail_ids": sorted(false_fail_ids),
    }
