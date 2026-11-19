"""Stand verified claims in for the history that no longer fits (the live half of claims-v2).

Phase 0 keeps the opening exchange and the newest messages and announces the gap. When the oldest part of that gap
has been compacted into a trusted chain of claim sets (core.context.compactor), this module puts those claims in the
gap, inside the same token budget, and announces only what is still uncovered. It changes nothing when the history
already fits, and it never raises into the caller's prompt path: every failure falls back to the Phase 0 result.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.context import compactor, store
from core.context.budget import FitResult, _omission_marker, estimate_tokens, fit_history

logger = logging.getLogger(__name__)

# The claims block may take at most this share of the history budget; the newest messages keep the rest.
CLAIMS_MAX_FRACTION = 0.15
_MIN_REMAINING_TOKENS = 1_000


def apply_claims(
    history: list[dict], fit: FitResult, budget_tokens: int, conversation_id: Optional[str], user_id: Optional[str]
) -> FitResult:
    """Return *fit* unchanged, or a re-fitted copy whose omitted prefix is covered by a claims block.

    Trust rules: every region must be a ``shadow_ok`` compaction of the current prompt version whose messages are
    byte-identical to the history now (content hash), forming one unbroken chain that starts right after the
    protected opening; only regions lying entirely inside the omitted messages are used. Corrections and
    supersession resolve across the used regions (the latest claim wins).
    """
    if not (fit.omitted and conversation_id and fit.head_count == compactor.HEAD_PROTECT):
        return fit
    if not compactor.claims_in_prompt_enabled():
        return fit
    items = [m for m in history if isinstance(m, dict) and m.get("content") not in (None, "")]
    if any(not m.get("id") for m in items):
        return fit                                    # messages without ids cannot be cited or matched
    rows = [(str(m["id"]), store.sha256_text(str(m["content"]))) for m in items]
    chain = compactor.chain_of(rows, store.list_compactions(conversation_id, user_id or ""), start=fit.head_count)
    if not chain:
        return fit

    omitted_ids = {str(m.get("id")) for m in fit.omitted}
    usable: list[dict] = []
    for item in chain:
        if not {rows[i][0] for i in range(item["first"], item["last"] + 1)} <= omitted_ids:
            break
        usable.append(item)
    if not usable:
        return fit

    results = [compactor._from_record(item["rec"]) for item in usable]
    cap = int(budget_tokens * CLAIMS_MAX_FRACTION)
    chosen = None
    for floor in (2, 3):                              # keep identifiers and values; fall back to decisions only
        for k in range(len(results), 0, -1):
            block = compactor.render_claims_block(results[:k], min_importance=floor)
            tokens = estimate_tokens(block) + 4
            if tokens <= cap:
                chosen = (k, floor, block, tokens)
                break
        if chosen:
            break
    if chosen is None or budget_tokens - chosen[3] < _MIN_REMAINING_TOKENS:
        return fit
    k, floor, block, tokens = chosen

    covered = {rows[i][0] for item in usable[:k] for i in range(item["first"], item["last"] + 1)}
    refit = fit_history(history, budget_tokens - tokens)          # the claims take their tokens from the newest tail
    still_omitted = {str(m.get("id")) for m in refit.omitted}
    if refit.head_count != fit.head_count or not covered <= still_omitted:
        return fit
    uncovered = [m for m in refit.omitted if str(m.get("id")) not in covered]
    refit.budget_tokens = budget_tokens
    refit.claims_block = block
    refit.marker = _omission_marker(uncovered, budget_tokens, after_head=True) if uncovered else None
    refit.tokens_after += tokens
    refit.claims_info = {
        "regions": k,
        "importance_floor": floor,
        "claims_shown": sum(1 for line in block.splitlines() if line.startswith("- ")),
        "tokens": tokens,
        "messages_covered": len(covered),
        "messages_uncovered": len(uncovered),
    }
    logger.info("context: claims in prompt conv=%s %s", conversation_id, refit.claims_info)
    return refit
