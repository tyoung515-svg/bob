"""BoBClaw Core — Action registry package (U3, SPEC-UI-OVERHAUL §3 / Decision D4).

Actions-as-data: one core-side registry, three frontends (palette · helper bubble · voice).
See ``core.actions.registry`` for the schema and seed set.
"""
from core.actions.registry import (
    AUTO_RISK_TIERS,
    RISK_TIERS,
    SEED_ACTIONS,
    Action,
    ActionBinding,
    ActionRegistry,
    get_default_registry,
)

__all__ = [
    "Action",
    "ActionBinding",
    "ActionRegistry",
    "AUTO_RISK_TIERS",
    "RISK_TIERS",
    "SEED_ACTIONS",
    "get_default_registry",
]
