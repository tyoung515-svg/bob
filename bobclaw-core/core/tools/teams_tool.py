"""BoBClaw Core — JOAT team-builder tools (DESIGN §6.4).

Read + write tools for the team-builder assistant: ``list_backends`` exposes the
backend palette (+ per-worker cost / fan-out width) so the assistant can compose a
fleet, and ``create_team`` persists a proposed team via the same validated path as
POST /api/teams. Backend STRINGS only — never model names.
"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from core import teams
from core.config import MAX_FANOUT_WIDTH_BY_BACKEND, MAX_WORKER_USD_BY_BACKEND


@tool
def list_backends() -> str:
    """List the registered bobclaw-core backends with their per-worker USD cost cap
    and max fan-out width — the palette for composing a JOAT team. Use these exact
    backend STRINGS (never model names) and the roles apex/worker/critic when
    proposing a team."""
    backends = [
        {
            "backend": b,
            "max_usd_per_worker": MAX_WORKER_USD_BY_BACKEND[b],
            "max_fanout_width": MAX_FANOUT_WIDTH_BY_BACKEND.get(b),
        }
        for b in sorted(MAX_WORKER_USD_BY_BACKEND)
    ]
    return json.dumps({"backends": backends, "roles": list(teams.ROLES)})


@tool
def create_team(name: str, roles: dict) -> str:
    """Create and persist a custom JOAT team.

    Args:
        name: lowercase slug (a-z, 0-9, hyphens); must not collide with a built-in.
        roles: ``{role: {backend, escalation_chain: [...]}}`` where role is
            apex|worker|critic and every backend is a string from ``list_backends``.

    Returns a success summary, or an ``"Error: ..."`` string on a validation failure
    (so the model can report it without raising).
    """
    try:
        created = teams.create_team(name, roles or {})
    except ValueError as exc:
        return f"Error: {exc}"
    role_summary = ", ".join(
        f"{role}=" + "/".join(s["backend"] for s in slots)
        for role, slots in created["roles"].items()
    )
    return f"Created team '{created['name']}' ({role_summary})."
