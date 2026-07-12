"""
BoBClaw Core — Action registry (U3, SPEC-UI-OVERHAUL §3 / Decision D4).

Actions-as-data: ONE core-side registry of user-invokable operations, surfaced through the
G1 ``/capabilities`` payload and consumed by THREE frontends off the same source of truth —
the MS8-A2 ``/`` command palette, the U5 "Ask Bob" helper bubble (tools), and voice (intents,
U11). Adding an action here lights it up on every frontend without touching any page.

Each action is a typed, fail-loud record::

    {id, title, description_plain, params_schema, risk, undo_hint, page_scope, binding}

* ``risk`` (D4/D11 tiers) — ``read`` (auto) · ``reversible`` (auto, incl. Simple mode) ·
  ``gated`` (never auto-executed; surfaces to Approvals with a plain-language explanation).
  An unknown tier is REJECTED with a specific error (mirrors the faces-registry pydantic
  discipline). A ``reversible`` action MUST carry an ``undo_hint`` (D12 guardrail).
* ``binding`` — the concrete existing op the action drives, so each seed id provably maps to a
  real gateway REST route or chat-WS control frame (asserted by the U3 mapping test). This is
  also the palette/helper wiring hook: the frontend reads ``binding`` to know what to call.

Read-only static data: no runtime state, no side effects on import.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Risk tiers (D4/D11). Kept as a plain tuple (not a Literal on the field) so the model can
# reject an unknown tier with a SPECIFIC, human-readable error instead of pydantic's generic
# "unexpected value" — the U3 accept criterion.
RISK_TIERS: tuple[str, ...] = ("read", "reversible", "gated")

# Tiers a frontend may execute WITHOUT routing through Approvals (D11).
AUTO_RISK_TIERS: frozenset[str] = frozenset({"read", "reversible"})


class ActionBinding(BaseModel):
    """The real, existing op an action drives. Exactly one transport:

    * ``kind="rest"`` — an HTTP op on the gateway: requires ``method`` + ``path`` (the gateway
      route template verbatim, e.g. ``/teams/{name}``).
    * ``kind="ws"``   — a chat-WebSocket control frame: requires ``ws_type`` (the ``type`` field
      the chat WS handler dispatches on, e.g. ``switch_face``).

    ``fixed_params`` pins parameters the caller must NOT vary (e.g. the ``deny`` action binds the
    shared Approvals decide op with ``{"decision": "reject"}``).
    """

    kind: str
    method: Optional[str] = None
    path: Optional[str] = None
    ws_type: Optional[str] = None
    fixed_params: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in ("rest", "ws"):
            raise ValueError(f"unknown binding kind {v!r}; must be 'rest' or 'ws'")
        return v

    @field_validator("method")
    @classmethod
    def _upper_method(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _transport_consistent(self) -> "ActionBinding":
        if self.kind == "rest":
            if not self.method or not self.path:
                raise ValueError("rest binding requires both 'method' and 'path'")
            if self.ws_type is not None:
                raise ValueError("rest binding must not set 'ws_type'")
        elif self.kind == "ws":
            if not self.ws_type:
                raise ValueError("ws binding requires 'ws_type'")
            if self.method is not None or self.path is not None:
                raise ValueError("ws binding must not set 'method'/'path'")
        return self


class Action(BaseModel):
    """One user-invokable action, as data (SPEC §3 schema + a concrete op ``binding``)."""

    id: str
    title: str
    description_plain: str
    params_schema: dict = Field(default_factory=dict)
    risk: str
    undo_hint: Optional[str] = None
    # Pages on which this action is offered (helper-bubble page-scoped tool filtering, §3).
    page_scope: list[str] = Field(default_factory=list)
    binding: ActionBinding

    @field_validator("id", "title", "description_plain")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip()

    @field_validator("risk")
    @classmethod
    def _known_risk(cls, v: str) -> str:
        if v not in RISK_TIERS:
            raise ValueError(
                f"unknown risk tier {v!r}; must be one of {', '.join(RISK_TIERS)}"
            )
        return v

    @model_validator(mode="after")
    def _reversible_has_undo(self) -> "Action":
        # D12 guardrail: a reversible-write must state how to undo it. gated actions never
        # auto-execute (they route to Approvals) and read actions mutate nothing, so neither
        # requires an undo_hint.
        if self.risk == "reversible" and not (self.undo_hint and self.undo_hint.strip()):
            raise ValueError(
                f"action {self.id!r} is reversible but has no undo_hint (D12)"
            )
        return self

    @property
    def auto(self) -> bool:
        """True if a frontend may execute this action without an Approvals gate (D11)."""
        return self.risk in AUTO_RISK_TIERS


# ─── Seed set (SPEC §3) ────────────────────────────────────────────────────────
# Each entry maps to a REAL existing gateway op (asserted by tests/…/test_capabilities_actions):
#   create_team      → POST   /teams                              (routers/teams)
#   delete_team      → DELETE /teams/{name}                       (routers/teams)
#   pin_face         → WS      switch_face                        (routers/chat)
#   switch_profile   → WS      switch_profile                     (routers/chat)
#   forget_fact      → DELETE /memory/facts/{fact_id}             (routers/memory)
#   new_conversation → POST   /conversations                      (routers/conversations)
#   approve          → POST   /approvals/{approval_id}/decide     (routers/approvals) decision=approve
#   deny             → POST   /approvals/{approval_id}/decide     (routers/approvals) decision=reject
SEED_ACTIONS: list[dict] = [
    {
        "id": "create_team",
        "title": "Create a team",
        "description_plain": "Create a new custom team from a set of roles.",
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Team name (unique; not a built-in)."},
                "roles": {
                    "type": "object",
                    "description": "role → {backend, escalation_chain} mapping.",
                },
                "overwrite": {"type": "boolean", "description": "Replace an existing custom team."},
            },
            "required": ["name", "roles"],
        },
        "risk": "reversible",
        "undo_hint": "Delete the team (delete_team) to undo.",
        "page_scope": ["teams"],
        "binding": {"kind": "rest", "method": "POST", "path": "/teams"},
    },
    {
        "id": "delete_team",
        "title": "Delete a team",
        "description_plain": "Delete a custom team. Built-in teams cannot be deleted.",
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the custom team to delete."},
            },
            "required": ["name"],
        },
        "risk": "reversible",
        "undo_hint": "Restore from the session-cached team YAML (re-create the team). (D12)",
        "page_scope": ["teams"],
        "binding": {"kind": "rest", "method": "DELETE", "path": "/teams/{name}"},
    },
    {
        "id": "pin_face",
        "title": "Pin a face",
        "description_plain": "Pin a specific Bob face to this conversation (skips auto-routing).",
        "params_schema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation to pin the face on."},
                "face_id": {
                    "type": "string",
                    "description": "Face id to pin; empty clears the pin (back to Auto).",
                },
                "face_name": {"type": "string", "description": "Optional display name for the pinned face."},
            },
            "required": ["conversation_id"],
        },
        "risk": "reversible",
        "undo_hint": "Clear the pin (pin_face with an empty face_id) to return to Auto, or re-pin the previous face.",
        "page_scope": ["chat"],
        "binding": {"kind": "ws", "ws_type": "switch_face"},
    },
    {
        "id": "switch_profile",
        "title": "Switch profile",
        "description_plain": "Pin a saved profile (e.g. a council shape) to this conversation.",
        "params_schema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation to apply the profile to."},
                "profile": {
                    "type": "string",
                    "description": "Profile name to pin; empty clears it.",
                },
            },
            "required": ["conversation_id"],
        },
        "risk": "reversible",
        "undo_hint": "Switch back to the previous profile (or send an empty profile to clear).",
        "page_scope": ["chat"],
        "binding": {"kind": "ws", "ws_type": "switch_profile"},
    },
    {
        "id": "forget_fact",
        "title": "Forget a memory fact",
        "description_plain": "Permanently delete a stored memory fact. This cannot be undone.",
        "params_schema": {
            "type": "object",
            "properties": {
                "fact_id": {"type": "string", "description": "Id of the memory fact to forget."},
            },
            "required": ["fact_id"],
        },
        # Irreversible data loss with no API undo path ⇒ gated: never auto-executed by the
        # helper/voice frontends; it surfaces to Approvals with a plain-language explanation.
        # (The dedicated Memory-page Forget button is a separate explicit op — D9 — not this
        # helper-governed tier.)
        "risk": "gated",
        "undo_hint": None,
        "page_scope": ["memory"],
        "binding": {"kind": "rest", "method": "DELETE", "path": "/memory/facts/{fact_id}"},
    },
    {
        "id": "new_conversation",
        "title": "New conversation",
        "description_plain": "Start a new, empty conversation with Bob.",
        "params_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Optional title (defaults to 'New Conversation')."},
                "face_id": {"type": "string", "description": "Optional face to open the conversation with."},
                "model_preference": {"type": "string", "description": "Optional model preference."},
                "project_id": {"type": "string", "description": "Optional project to file the conversation under."},
            },
            "required": [],
        },
        "risk": "reversible",
        "undo_hint": "Delete the new conversation to undo.",
        "page_scope": ["home", "chat"],
        "binding": {"kind": "rest", "method": "POST", "path": "/conversations"},
    },
    {
        "id": "approve",
        "title": "Approve a pending action",
        "description_plain": "Approve a pending gated action so Bob may carry it out.",
        "params_schema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string", "description": "Id of the pending approval."},
                "edit_content": {
                    "type": "string",
                    "description": "Optional human-edited content applied on approval (e.g. an edited diff).",
                },
            },
            "required": ["approval_id"],
        },
        # A consequential decision: never auto-fired by the helper/voice — a human clicks it.
        "risk": "gated",
        "undo_hint": None,
        "page_scope": ["approvals"],
        "binding": {
            "kind": "rest",
            "method": "POST",
            "path": "/approvals/{approval_id}/decide",
            "fixed_params": {"decision": "approve"},
        },
    },
    {
        "id": "deny",
        "title": "Deny a pending action",
        "description_plain": "Reject a pending gated action so Bob will not carry it out.",
        "params_schema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string", "description": "Id of the pending approval."},
            },
            "required": ["approval_id"],
        },
        "risk": "gated",
        "undo_hint": None,
        "page_scope": ["approvals"],
        "binding": {
            "kind": "rest",
            "method": "POST",
            "path": "/approvals/{approval_id}/decide",
            "fixed_params": {"decision": "reject"},
        },
    },
]


# ─── Registry ──────────────────────────────────────────────────────────────────

class ActionRegistry:
    """Loads and indexes Action records (defaults to the SPEC §3 seed set)."""

    def __init__(self, actions: Optional[list] = None) -> None:
        raw = SEED_ACTIONS if actions is None else actions
        self._actions: dict[str, Action] = {}
        for entry in raw:
            action = entry if isinstance(entry, Action) else Action.model_validate(entry)
            if action.id in self._actions:
                raise ValueError(f"duplicate action id: {action.id!r}")
            self._actions[action.id] = action

    # ── public API ──────────────────────────────────────────────────────────────

    def get(self, action_id: str) -> Action:
        """Return the Action for *action_id*, or raise KeyError."""
        try:
            return self._actions[action_id]
        except KeyError:
            raise KeyError(f"Unknown action id: '{action_id}'") from None

    def list_actions(self) -> list[Action]:
        """Return every Action, ordered by id for stable output."""
        return [self._actions[aid] for aid in sorted(self._actions)]

    def as_payload(self) -> list[dict]:
        """Return the registry as JSON-serializable dicts (the ``/capabilities`` actions section)."""
        return [a.model_dump() for a in self.list_actions()]

    def __len__(self) -> int:
        return len(self._actions)

    def __contains__(self, action_id: str) -> bool:
        return action_id in self._actions


# ─── Module-level singleton accessor ───────────────────────────────────────────
_DEFAULT_REGISTRY: Optional["ActionRegistry"] = None


def get_default_registry() -> "ActionRegistry":
    """Return the lazily-constructed module-level ActionRegistry singleton."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ActionRegistry()
    return _DEFAULT_REGISTRY
