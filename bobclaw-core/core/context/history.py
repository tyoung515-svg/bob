"""Build the ``Prior conversation`` system block from the gateway's history list."""
from __future__ import annotations

import logging
from typing import Optional

from core.context.budget import fit_history, render_prior_conversation
from core.context.windows import fit_enabled, history_budget_tokens, limits_for

logger = logging.getLogger(__name__)


def build_prior_conversation(
    history: list[dict],
    *,
    preferred_backend: Optional[str] = None,
    backend_override: Optional[str] = None,
    conversation_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> tuple[str, Optional[dict]]:
    """Return ``(system_message_text, fit_metadata)`` for *history*.

    The backend is only a hint here (routing happens later): the explicit override, else the face's
    preferred backend. ``enforce_backend_limits`` re-checks against the backend actually called.

    When messages must be omitted and the conversation has a trusted chain of compacted claims, those claims stand
    in for the oldest omitted messages (core.context.assembler). Any failure there falls back to the plain note.
    """
    if not fit_enabled():
        flat = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        return f"Prior conversation:\n{flat}", None

    backend = backend_override or preferred_backend
    limits = limits_for(backend)
    budget = history_budget_tokens(limits)
    fit = fit_history(history, budget)
    if fit.omitted and conversation_id:
        try:
            from core.context.assembler import apply_claims

            fit = apply_claims(history, fit, budget, conversation_id, user_id)
        except Exception:
            logger.warning("context: claims assembly failed; using the plain omission note", exc_info=True)
    meta = {
        "backend": backend,
        "window_tokens": limits.window_tokens,
        "budget_tokens": budget,
        "messages_in": len(history),
        "messages_kept": len(fit.kept),
        "messages_omitted": len(fit.omitted),
        "messages_truncated": len(fit.truncated_ids),
        "tokens_est_before": fit.tokens_before,
        "tokens_est_after": fit.tokens_after,
    }
    if fit.claims_info:
        meta["claims"] = dict(fit.claims_info)
    return render_prior_conversation(fit), meta
