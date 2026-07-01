from __future__ import annotations

from core.ledger.types import MergeDecision

def merge_decision(
    verdicts: list[dict],
    budget_escalated: bool = False,
) -> dict:
    """Determine the merge gate decision according to §2.9 precedence rules.

    Args:
        verdicts: List of verdict dicts, each with keys 'bid_key', 'verified', 'exhausted'.
        budget_escalated: If True, overrides all other rules -> ESCALATE.

    Returns:
        Dict with "decision" (MergeDecision value string) and "reasons" (list of strings).
    """
    if budget_escalated:
        return {
            "decision": MergeDecision.ESCALATE.value,
            "reasons": ["budget escalation: contested by cost"],
        }

    if not verdicts:
        return {
            "decision": MergeDecision.REVERT.value,
            "reasons": ["default-fail: no verdicts / no evidence"],
        }

    # Fail-closed (Default-FAIL): a verdict counts as satisfied ONLY if it is explicitly
    # verified or explicitly exhausted-tagged. A verdict missing those keys, or with None,
    # is treated as unverified (REVERT) — never silently passed to FAST_FORWARD. Use .get()
    # for bid_key so a malformed verdict reverts instead of raising KeyError.
    failing_keys = []
    for v in verdicts:
        satisfied = v.get("verified") is True or v.get("exhausted") is True
        if not satisfied:
            failing_keys.append(v.get("bid_key"))

    if failing_keys:
        reasons = [f"unverified non-exhausted: {key}" for key in failing_keys]
        return {
            "decision": MergeDecision.REVERT.value,
            "reasons": reasons,
        }

    return {
        "decision": MergeDecision.FAST_FORWARD.value,
        "reasons": ["all criteria verified or exhausted-tagged"],
    }

def is_fast_forwardable(verdicts: list[dict], budget_escalated: bool = False) -> bool:
    """Check if the given verdicts permit a fast-forward merge.

    Mirrors ``merge_decision``'s signature so budget escalation is reflected — an escalated
    merge is never fast-forwardable.

    Args:
        verdicts: List of verdict dicts as accepted by merge_decision.
        budget_escalated: Forwarded to merge_decision (escalation => not fast-forwardable).

    Returns:
        True if merge_decision returns FAST_FORWARD, else False.
    """
    return (
        merge_decision(verdicts, budget_escalated)["decision"]
        == MergeDecision.FAST_FORWARD.value
    )
