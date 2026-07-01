"""BoBClaw Core — team-builder assistant (propose-a-fill, DESIGN §6.4).

Given a free-text goal, ask the team-builder backend for a JOAT team proposal
(role→backend) drawn from the live backend palette, parse it, and return a
validated proposal the UI fills into the builder form for review + Save (it never
auto-saves). The backend call is injectable so the propose path is unit-testable
without network.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Optional

from core import teams
from core.config import KNOWN_BACKENDS, MAX_WORKER_USD_BY_BACKEND

logger = logging.getLogger(__name__)

# The assistant-tools worker tier (a tool-capable cloud worker).
_PROPOSER_BACKEND = "deepseek_v4_flash"

_SYSTEM = (
    "You are the BoBClaw team-builder. Compose a JOAT 'team': a fleet binding the "
    "roles apex (manager), worker (bulk), and critic (fast reviewer) to backends. "
    "Reply with ONLY a single JSON object and nothing else."
)


def _palette_line() -> str:
    return ", ".join(
        f"{b} (${MAX_WORKER_USD_BY_BACKEND[b]:.3f}/worker)"
        for b in sorted(MAX_WORKER_USD_BY_BACKEND)
    )


def _build_prompt(goal: str) -> str:
    return (
        f"Goal: {goal.strip() or 'a balanced general-purpose fleet'}\n\n"
        f"Available backends (use these EXACT strings, never model names): "
        f"{_palette_line()}\n"
        f"Roles: apex, worker, critic.\n\n"
        "Reply with ONLY this JSON shape:\n"
        '{"name": "<lowercase-slug>", "roles": {"apex": {"backend": "<b>", '
        '"escalation_chain": ["<b>"]}, "worker": {...}, "critic": {...}}}'
    )


def _extract_json(text: str) -> Optional[dict]:
    """Parse the first balanced ``{...}`` object in the reply (LLMs often wrap JSON
    in prose). Returns None if no parseable object is present."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _sanitize_roles(raw_roles) -> dict:
    """Keep only apex/worker/critic mapped to a KNOWN backend; filter escalation
    hops to known backends. Drops anything invalid so the proposal is buildable."""
    out: dict[str, dict] = {}
    if not isinstance(raw_roles, dict):
        return out
    for role in teams.ROLES:
        spec = raw_roles.get(role)
        if not isinstance(spec, dict):
            continue
        backend = spec.get("backend")
        if backend not in KNOWN_BACKENDS:
            continue
        chain = [b for b in (spec.get("escalation_chain") or []) if b in KNOWN_BACKENDS]
        out[role] = {"backend": backend, "escalation_chain": chain}
    return out


async def propose_team(
    goal: str,
    *,
    send: Optional[Callable[[list, str], Awaitable[str]]] = None,
) -> dict:
    """Return ``{goal, name, roles, raw}`` (plus ``error`` on a backend failure).

    ``send`` defaults to ``execute._send_to_backend`` (injected in tests). ``roles``
    is sanitized to known backends; ``name`` is a suggested lowercase slug (possibly
    empty). NEVER persists — the user reviews the filled form and Saves.
    """
    if send is None:
        from core.nodes.execute import _send_to_backend as send  # lazy: avoid cycle
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _build_prompt(goal)},
    ]
    try:
        raw = await send(messages, _PROPOSER_BACKEND)
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the request
        logger.warning("team proposer backend failed: %s", exc)
        return {"goal": goal, "name": "", "roles": {}, "raw": "", "error": str(exc)}
    parsed = _extract_json(raw or "") or {}
    name = parsed.get("name") if isinstance(parsed.get("name"), str) else ""
    return {
        "goal": goal,
        "name": (name or "").strip().lower(),
        "roles": _sanitize_roles(parsed.get("roles")),
        "raw": raw or "",
    }


# ── Multi-turn refine (DESIGN §6.4 — a few rounds to shape the profile) ──────────

_REFINE_SYSTEM = (
    "You are the BoBClaw profile-builder. A 'profile' binds roles to backends AND "
    "instructs how they work together. Roles are apex (manager), worker (bulk), critic "
    "(fast reviewer); EACH role is a LIST of slots (a role may bind more than one "
    "backend). Each slot may carry a 'role_prompt' — a short editable instruction for "
    "that spot's angle/tone.\n\n"
    "Optional coordination: 'shape' = fusion (parallel voices reconcile) | sequential "
    "(chain) | debate; and 'protocol_bounds' = {max_rounds, max_usd, grounding:on|off}. "
    "Omit shape for a plain routing roster.\n\n"
    "Every turn: (1) briefly say what you changed (1-2 sentences), THEN (2) output the FULL "
    "updated profile as a SINGLE JSON object in this shape:\n"
    '{"name":"<slug>","roles":{"worker":[{"name":"bulk","backend":"<b>",'
    '"escalation_chain":["<b>"],"role_prompt":"<how this spot acts>"}]},'
    '"shape":"fusion","protocol_bounds":{"max_usd":2.0}}\n\n'
    "RECOMMENDED (guidance — experiment freely, NOT hard limits): apex 1 manager-tier; "
    "worker 1-3 cost-efficient; critic 1 fast/local; escalation 0-2 deep; keep role_prompts "
    "short. For Claude, prefer claude_code (CC) as the PRIMARY and claude_api only as an "
    "escalation/fallback. Use ONLY these backend strings (never model names): "
)


def _refine_system() -> str:
    return _REFINE_SYSTEM + _palette_line() + "."


def _sanitize_draft(parsed) -> dict:
    """Coerce a parsed model object to a valid multi-slot draft: roles → list of
    {name, backend, escalation_chain} slots with KNOWN backends only (drops the rest)."""
    if not isinstance(parsed, dict):
        return {"name": "", "roles": {}}
    raw_name = parsed.get("name")
    name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
    roles: dict[str, list] = {}
    raw_roles = parsed.get("roles")
    if isinstance(raw_roles, dict):
        for role in teams.ROLES:
            valid = []
            for slot in teams._as_slots(raw_roles.get(role)):
                if slot["backend"] not in KNOWN_BACKENDS:
                    continue
                valid.append({
                    "name": slot["name"],
                    "backend": slot["backend"],
                    "escalation_chain": [e for e in slot["escalation_chain"] if e in KNOWN_BACKENDS],
                    "role_prompt": slot["role_prompt"],
                })
            if valid:
                roles[role] = valid
    draft: dict = {"name": name, "roles": roles}
    # Profile (HOW) fields — optional; kept only when valid.
    shape = parsed.get("shape")
    if isinstance(shape, str) and shape in teams._PROFILE_SHAPES:
        draft["shape"] = shape
    synth = parsed.get("synth_backend")
    if isinstance(synth, str) and synth in KNOWN_BACKENDS:
        draft["synth_backend"] = synth
    bounds = parsed.get("protocol_bounds")
    if isinstance(bounds, dict):
        clean = {
            k: v for k, v in bounds.items()
            if k in ("max_rounds", "restart_budget", "drift_threshold", "max_usd", "grounding")
            and isinstance(v, (int, float, str, bool))
        }
        if clean:
            draft["protocol_bounds"] = clean
    return draft


def _draft_json(draft) -> str:
    if not draft or not draft.get("roles"):
        return "(none yet)"
    return json.dumps(draft)


def _reply_prose(text: str) -> str:
    """The natural-language part of the reply (everything before the JSON object)."""
    idx = text.find("{")
    prose = (text[:idx] if idx != -1 else text).strip()
    return prose or "Updated the draft."


async def refine_team(
    message: str,
    *,
    history: Optional[list] = None,
    draft: Optional[dict] = None,
    send: Optional[Callable[[list, str], Awaitable[str]]] = None,
) -> dict:
    """One refinement turn → ``{reply, draft, raw}`` (plus ``error`` on failure).

    ``history`` is the prior ``[{role, content}]`` chat; ``draft`` is the current team.
    Stateless — the client threads history/draft each turn. The model gets the SHAPE +
    a recommended envelope (no static cap) and experiments within it. Never persists.
    """
    if send is None:
        from core.nodes.execute import _send_to_backend as send  # lazy: avoid cycle
    msgs = [{"role": "system", "content": _refine_system()}]
    for turn in (history or []):
        role = turn.get("role") if isinstance(turn, dict) else None
        content = turn.get("content") if isinstance(turn, dict) else None
        if role in ("user", "assistant") and isinstance(content, str):
            msgs.append({"role": role, "content": content})
    instruction = (message or "").strip() or "Propose a balanced general-purpose fleet."
    msgs.append({
        "role": "user",
        "content": f"Current draft:\n{_draft_json(draft)}\n\nInstruction: {instruction}",
    })
    fallback = draft if (draft and draft.get("roles")) else {"name": "", "roles": {}}
    try:
        raw = await send(msgs, _PROPOSER_BACKEND)
    except Exception as exc:  # noqa: BLE001
        logger.warning("team refine backend failed: %s", exc)
        return {"reply": f"(assistant error: {exc})", "draft": fallback, "raw": "", "error": str(exc)}
    parsed = _extract_json(raw or "")
    new_draft = _sanitize_draft(parsed) if parsed is not None else fallback
    # If the model replied but produced no usable roles, keep the prior draft.
    if not new_draft.get("roles"):
        new_draft = fallback if fallback.get("roles") else new_draft
    return {"reply": _reply_prose(raw or ""), "draft": new_draft, "raw": raw or ""}
