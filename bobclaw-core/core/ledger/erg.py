from __future__ import annotations

from copy import deepcopy

from core.ledger.types import (
    RETRY_LIMIT,
    EXHAUSTED_TAG,
    RetryReason,
    ErgAction,
    ClaimStatus,
)

"""
Externalized Retry-Gate state machine (§2.6 F1).

Provides pure functions for deciding on retries, validating critic reasons,
building rejection signals, and handling entailment failures. All functions
are deterministic and produce JSON-serializable outputs.
"""


def next_action(retry_count: int) -> str:
    """Return the ERG action string based on retry_count.

    If retry_count < RETRY_LIMIT, yields RE_BRANCH; otherwise EXHAUSTED_SEARCH.
    """
    if retry_count < RETRY_LIMIT:
        return ErgAction.RE_BRANCH.value
    return ErgAction.EXHAUSTED_SEARCH.value


def validate_reason(reason) -> bool:
    """Return True iff reason is None or a valid RetryReason (string or enum)."""
    if reason is None:
        return True
    try:
        RetryReason(reason)
        return True
    except (ValueError, TypeError):
        return False


def build_rejection_signal(bid_key: str, tried_sources: list[str]) -> str:
    """Build the standard rejection signal string.

    Format: [REJECTED: <bid_key> | <comma-separated tried_sources>]
    """
    return f"[REJECTED: {bid_key} | {', '.join(tried_sources)}]"


def on_entailment_failure(entry: dict, new_source: str, reason=None) -> dict:
    """Process an entailment failure and produce updated entry + directive.

    The input entry is deep-copied to avoid mutation. Returns a dict with keys
    'entry' (updated copy) and 'directive' (dict with action and metadata).
    """
    # Deep copy to prevent mutation of caller's dict
    updated = deepcopy(entry)

    # Compute new retry count and deduplicated tried_sources (first-seen order)
    new_retry_count = updated["retry_count"] + 1
    deduped = []
    seen = set()
    for src in updated["tried_sources"] + [new_source]:
        if src not in seen:
            seen.add(src)
            deduped.append(src)
    updated["retry_count"] = new_retry_count
    updated["tried_sources"] = deduped

    if new_retry_count < RETRY_LIMIT:
        # Re-branch: keep status unchanged (PENDING expected)
        bid = updated["bid_key"]
        directive = {
            "action": ErgAction.RE_BRANCH.value,
            "bid_key": bid,
            "tried_sources": deduped,
            "constraint": build_rejection_signal(bid, deduped)
            + " retrieve a strictly decorrelated source not in this list",
            "reason": reason if (validate_reason(reason) and reason is not None) else None,
        }
    else:
        # Exhausted: mark status as UNVERIFIED_EXHAUSTED
        updated["status"] = ClaimStatus.UNVERIFIED_EXHAUSTED.value
        directive = {
            "action": ErgAction.EXHAUSTED_SEARCH.value,
            "status_tag": EXHAUSTED_TAG,
        }

    return {"entry": updated, "directive": directive}
