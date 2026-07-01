"""
BoBClaw Core — Permission and tool-access checker

Stateless helpers used by the graph to decide:
  • whether a face is allowed to use a specific tool
  • whether a given action type always requires human approval
  • whether a scoped job's action is auto-cleared, gate-routed, or human-routed
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import re
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field

# ─── Action types that always require approval ────────────────────────────────

_APPROVAL_REQUIRED: frozenset[str] = frozenset(
    {
        "email_send",
        "email_reply",
        "form_submit",
        "purchase",
        "file_delete",
        "shell_dangerous",
        # C4: a Claude-Code planner proposed a code edit (unified diff). It is
        # captured + parked here and applied only after a human (or, later, the
        # Gate router via route_approval) approves.
        "cc_edit",
    }
)

# Always-human floor for the Gate router. These actions can never be auto-cleared by a
# scope, regardless of what the job spec claims — the genuinely IRREVERSIBLE / OUTWARD
# set (sends, purchases, deletes, shell, and the final merge).
#
# NOTE (Neck Beard P3): this is now an EXPLICIT set, no longer ``_APPROVAL_REQUIRED |
# {merge_to_main}``. ``cc_edit`` is deliberately NOT here so a headless agent holding a
# token scoped ``cc_edit ∈ auto_actions`` + the path in ``may_touch`` can auto-clear an
# in-scope edit (the core Neck Beard value). ``cc_edit`` STAYS in ``_APPROVAL_REQUIRED``,
# so without a scope it still routes to a human; and the actual apply is independently
# gated by ``CC_EDIT_APPLY_ENABLED`` (default off) — a no-commit, reversible working-tree
# ``git apply``. The irreversible actions above remain always-human.
_ALWAYS_HUMAN_ACTIONS: frozenset[str] = frozenset(
    {
        "email_send",
        "email_reply",
        "form_submit",
        "purchase",
        "file_delete",
        "shell_dangerous",
        "merge_to_main",
    }
)

# ─── Keyword patterns that suggest a dangerous action in free text ──────────
# (Used by execute node to auto-detect approval requirement from the task string)

_DANGEROUS_PATTERNS = re.compile(
    r"\b("
    r"send\s+email|reply\s+to|send\s+reply|email\s+send"
    r"|submit\s+form|fill\s+out\s+form"
    r"|place\s+order|purchase|buy\s+now|checkout"
    r"|rm\s+-rf|delete\s+file|remove\s+file|drop\s+table"
    r"|execute\s+shell|run\s+script"
    r")\b",
    re.IGNORECASE,
)

# ─── Gate Router scope model ──────────────────────────────────────────────────
# See tasks/2026-06-15-gate-router/INTAKE.md for the full design.

class Scope(BaseModel):
    """Machine-readable blast radius for a Gate-governed job."""

    branch: Optional[str] = None
    may_touch: list[str] = Field(default_factory=list)
    may_not_touch: list[str] = Field(default_factory=list)
    auto_actions: list[str] = Field(default_factory=list)
    escalate_actions: list[str] = Field(default_factory=list)
    budget_usd: Optional[float] = None


# ─── Gate Router policy ───────────────────────────────────────────────────────

_LiteralDest = str  # one of "auto" | "gate" | "human"


def evaluate_action(action_type: str, scope: Optional[Scope]) -> _LiteralDest:
    """Route an action type to its Gate destination.

    Returns one of:
      * "auto"  — action is pre-approved by the job scope.
      * "gate"  — action is ambiguous or explicitly escalated; run the critic.
      * "human" — action is destructive, out-of-scope, or the floor matched.

    Fail closed: missing scope or unknown action → "human" when a floor applies,
    otherwise "gate".
    """
    if not action_type:
        return "human"
    if action_type in _ALWAYS_HUMAN_ACTIONS:
        return "human"
    if scope is None:
        return "human"
    if action_type in scope.auto_actions:
        return "auto"
    if action_type in scope.escalate_actions:
        return "gate"
    # Unknown action: fail closed to the critic, not straight to a human.
    return "gate"


def is_path_within(path: str, root: str) -> bool:
    """True iff *path* resolves to a location inside (or equal to) *root*.

    The hard filesystem containment check for the build sandbox (P3): both sides are
    fully resolved (symlinks + ``..`` collapsed) BEFORE comparison, so a crafted
    ``..``/symlink path that textually looks inside ``root`` but resolves outside is
    rejected. Fail CLOSED — any error (bad path, unresolvable) returns False. This is
    the deterministic boundary; :func:`evaluate_path` is the Gate-Router *action*
    router (auto/gate/human), a separate concern from raw containment.
    """
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return p == r or r in p.parents


def evaluate_path(path: str, scope: Optional[Scope]) -> _LiteralDest:
    """Route a file path to its Gate destination based on the job scope.

    Returns one of:
      * "auto"  — path matches an explicit may_touch pattern.
      * "gate"  — path is not covered by the scope (ambiguous).
      * "human" — path matches an explicit may_not_touch pattern.

    Fail closed: missing scope or no match → "gate".
    """
    if not path:
        return "gate"
    if scope is None:
        return "gate"
    for pattern in scope.may_not_touch:
        if fnmatch.fnmatch(path, pattern):
            return "human"
    for pattern in scope.may_touch:
        if fnmatch.fnmatch(path, pattern):
            return "auto"
    return "gate"

# ─── Gateway→core scope vouch (Neck Beard P3) ─────────────────────────────────
# Core has no auth of its own — it trusts the gateway. So a scope claim arriving at
# core /api/chat must be ATTESTED by the gateway (which authenticated the agent token),
# or a direct-to-core caller could self-assert a fat scope and auto-grant destructive
# actions. The attestation is an HMAC over the canonical scope keyed by the shared
# BOBCLAW_SECRET: only the gateway (which knows the secret) can mint it, and it is bound
# to the EXACT scope bytes, so a captured vouch can't be replayed against a wider scope.
#
# The two string-level primitives (compute/verify) were built through the BoBClaw build
# pipe and Docker-verified against fixed HMAC vectors (tasks/2026-06-19-neckbeard-mode/
# p3_buildpipe_trial.py); the Scope-binding wrappers below are the security boundary and
# are hand-authored.


def canonical_scope_json(scope: "Union[dict, Scope]") -> str:
    """Canonical JSON for a scope, normalized through the :class:`Scope` model.

    Validating to ``Scope`` (then dumping) makes the canonical form independent of the
    incoming dict's key ORDER and of any extra keys, so the gateway and core feed
    byte-identical input to the HMAC regardless of JSON round-tripping. Field order is
    the model's declaration order (deterministic for a given Scope version — gateway and
    core deploy together). Raises ``pydantic.ValidationError`` on a malformed scope; the
    wrappers below catch it and fail closed.
    """
    model = scope if isinstance(scope, Scope) else Scope.model_validate(scope)
    return model.model_dump_json()


def compute_scope_vouch(canonical: str, secret: str) -> str:
    """HMAC-SHA256 hex of *canonical* keyed by *secret* (both UTF-8); ``""`` if *secret*
    is falsy.

    The empty-secret fail-closed return means a mis-set / empty ``BOBCLAW_SECRET`` can
    never produce a usable (forgeable, fixed-key) vouch — verification then always fails
    and scope ingress degrades to human-gated, never auto-granted. TOTAL function:
    non-str / falsy inputs return ``""`` rather than raising.
    """
    if not isinstance(canonical, str) or not isinstance(secret, str) or not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_scope_vouch(canonical: str, vouch: str, secret: str) -> bool:
    """Constant-time check that *vouch* matches the HMAC of *canonical* under *secret*.

    Fail closed: ``False`` when *secret* or *vouch* is falsy/NON-STRING (or the secret
    yields an empty digest). The ``isinstance`` guard matters because *vouch* is the
    attacker-controlled field on the wire — a non-str (``[]`` / ``{}`` / int) would
    otherwise raise ``TypeError`` out of :func:`hmac.compare_digest` instead of failing
    closed. Uses :func:`hmac.compare_digest` so a wrong vouch leaks no timing.
    """
    if not isinstance(canonical, str) or not isinstance(vouch, str) or not isinstance(secret, str):
        return False
    if not secret or not vouch:
        return False
    expected = compute_scope_vouch(canonical, secret)
    if not expected:
        return False
    return hmac.compare_digest(expected, vouch)


def scope_vouch(scope: "Union[dict, Scope]", secret: str) -> str:
    """Gateway helper: mint a vouch for *scope* (canonicalized via the model).

    ``""`` (no vouch) on a malformed scope or falsy secret — the receiver then fails
    closed and the scope is not honored.
    """
    try:
        canonical = canonical_scope_json(scope)
    except Exception:  # noqa: BLE001 — malformed scope ⇒ no vouch (fail closed)
        return ""
    return compute_scope_vouch(canonical, secret)


def verify_scope(scope: "Union[dict, Scope]", vouch: str, secret: str) -> bool:
    """Core helper: is *vouch* a valid gateway attestation of *scope* under *secret*?

    Fail closed on a malformed scope, an empty secret/vouch, or a mismatch.
    """
    try:
        canonical = canonical_scope_json(scope)
    except Exception:  # noqa: BLE001 — malformed scope ⇒ reject (fail closed)
        return False
    return verify_scope_vouch(canonical, vouch, secret)


# ─── Face-level tool allowlists (single source of truth: FaceRegistry) ────────
# Populated lazily from FaceRegistry. If the registry cannot be loaded, fall
# back to an empty list so a face cannot accidentally inherit stale fantasy
# labels from a second copy.

_FACE_TOOL_CACHE: dict[str, list[str]] = {}


def _get_allowed_tools(face_id: str) -> list[str]:
    """Return allowed tools for a face, consulting registry first."""
    if face_id in _FACE_TOOL_CACHE:
        return _FACE_TOOL_CACHE[face_id]
    try:
        from core.faces.registry import get_default_registry
        registry = get_default_registry()
        tools = registry.get_allowed_tools(face_id)
        _FACE_TOOL_CACHE[face_id] = tools
        return tools
    except Exception:
        return []


# ─── Public API ───────────────────────────────────────────────────────────────

def check_tool_access(face_id: str, tool_name: str) -> bool:
    """Return True if *face_id* is permitted to use *tool_name*."""
    allowed = _get_allowed_tools(face_id)
    return tool_name in allowed


def requires_approval(action_type: str) -> bool:
    """Return True if *action_type* must be approved by a human before execution."""
    return action_type in _APPROVAL_REQUIRED


def task_requires_approval(task_text: str) -> bool:
    """Heuristic: scan free-text task for patterns that imply a dangerous action."""
    return bool(_DANGEROUS_PATTERNS.search(task_text))
