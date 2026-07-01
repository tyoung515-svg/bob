"""
Shared per-run bound coercion for the council loops (grounding + debate).

A profile's ``protocol_bounds`` only overrides what it sets; an unset/None/malformed
value falls back to the global default. Lifted out of ``grounding.py`` so ``debate.py``
can reuse it without a debate→grounding import.
"""
from __future__ import annotations


def bound_float(value, default: float) -> float:
    """Coerce a per-profile numeric bound to float, falling back to ``default``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bound_int(value, default: int) -> int:
    """Int variant of :func:`bound_float` (e.g. restart_budget / max_rounds)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
