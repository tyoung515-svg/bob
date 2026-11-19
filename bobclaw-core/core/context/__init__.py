"""Context management: per-backend windows, history fitting, transport guards."""
from core.context.budget import (
    FitResult,
    enforce_backend_limits,
    estimate_tokens,
    fit_history,
    render_prior_conversation,
    trim_prior_conversation_text,
)
from core.context.history import build_prior_conversation
from core.context.windows import (
    BackendLimits,
    fit_enabled,
    history_budget_tokens,
    is_cloud_backend,
    limits_for,
    prompt_char_cap,
)

__all__ = [
    "BackendLimits",
    "FitResult",
    "build_prior_conversation",
    "enforce_backend_limits",
    "estimate_tokens",
    "fit_enabled",
    "fit_history",
    "history_budget_tokens",
    "is_cloud_backend",
    "limits_for",
    "prompt_char_cap",
    "render_prior_conversation",
    "trim_prior_conversation_text",
]
