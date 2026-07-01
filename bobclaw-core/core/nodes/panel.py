"""
BoBClaw — CoCouncil fusion panel (P1b).

Wires P1a's council engine (``core/council/engine.py``) into Bob's LangGraph for
the **fusion** shape: all seats answer the same prompt blind, in parallel, then
``synthesize_node`` reconciles them into one answer + handoff.

This module mirrors ``core/nodes/dispatch.py`` (the Send replication template):

  * ``panel_dispatch_node``  — state-mutation node. Reads ``council_spec`` (seats
    + mode), resolves each seat's backend per design table E, writes the resolved
    seat list back onto ``council_spec["resolved_seats"]``. Analogous to
    ``dispatch_node`` setting up fan-out params.
  * ``_route_after_panel``   — conditional edge. Returns one
    ``Send("panel_worker", {...})`` per resolved seat (the replication Send),
    each carrying the SAME shared task (fusion = all seats answer blind), its
    posture/backend/fallback chain, and ``seat_idx``. Mirrors
    ``_route_after_dispatch``.
  * ``panel_worker_node``    — single-seat call (mirrors ``worker_node``). Tries
    the seat backend, then the fallback chain on error, and appends one entry to
    ``panel_results`` (reducer ``operator.add``; each entry carries ``idx`` so
    ``synthesize_node`` can sort deterministically).

The backend seam (``make_backend_fn`` / ``make_cost_fn``) lives here and is also
imported by ``core/nodes/council.py`` (the sequential shape) — it adapts Bob's
``execute._send_to_backend`` + ``backends/_cost`` to the engine's injected
``BackendFn`` / ``cost_fn`` signatures.

P1b only: NO Chair, NO debate loop, NO grounding gate, NO budgets. Those are
P2–P6. Network-mockable exactly like the critic (tests patch ``_send_to_backend``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Union

from langgraph.types import Send

from core.config import (
    COUNCIL_DEFAULT_SEATS,
    COUNCIL_SEAT_BACKENDS,
    WORKER_TIMEOUT_SECONDS,
)
from core.council.engine import BackendFn, CostFn
from core.council.protocol import _PROTOCOLS_SUMMARY_TEMPLATE, load_protocols
from core.nodes.execute import _send_to_backend

logger = logging.getLogger(__name__)

_COUNCIL_SYSTEM_BASE = (
    "You are participating in a ForestOS council session governed by COUNCIL-OS v1.0. "
    "Follow all ratified protocols strictly. "
    "Do not summarize or restate prior content — delta-only per [PROT-01]."
)


# ── Backend / cost seam (the key P1b rewire) ─────────────────────────────────

def make_backend_fn(backend: str) -> BackendFn:
    """Adapt a Bob backend name to the engine's ``BackendFn`` seam.

    The engine calls backends as ``async (system: str, user_msg: str) -> str``.
    Bob's seam is ``_send_to_backend(messages: list[dict], backend: str) -> str``
    (the same async injection point ``critic.py`` and ``worker.py`` use), so
    tests patching ``_send_to_backend`` mock the council with zero network.
    """
    async def _fn(system: str, user_msg: str) -> str:
        return await _send_to_backend(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            backend,
        )

    return _fn


def make_cost_fn() -> Optional[CostFn]:
    """Adapt Bob's cost metering to the engine's ``cost_fn`` hook, or None.

    The engine's ``cost_fn`` is ``(model/backend name, token_count) -> usd``.
    ``core/backends/_cost.py`` exposes daily-spend tracking + per-call caps but
    NO clean per-(name, tokens) price function, and the design explicitly says
    NOT to invent a price map (the stale ``claude-opus-4-6``/``gemini-2.0-flash``
    map was dropped in the port). So P1b passes ``cost_fn=None`` (per-session
    cost 0.0 / metering skipped). TODO(P2): wire a real per-(backend, tokens)
    estimator once the budget/ceiling work lands (design §A3).
    """
    return None


# ── Seat → backend selector (design table E) ─────────────────────────────────

def resolve_seat_backend(
    posture: str, profile: Optional[dict] = None
) -> tuple[str, list[str], str]:
    """Map a seat *posture* to ``(backend, fallback_chain)`` per design table E.

    Reads the static default map ``COUNCIL_SEAT_BACKENDS`` from config (P1b;
    health/cost selection is P2 and per-profile YAML is P4). A *profile* override
    dict — shaped ``{posture: {"backend": ..., "fallback_chain": [...]}}`` — wins
    over the table when it carries the posture, so the P4 profile builder can
    drop in without touching this signature.

    Unknown postures fall back to the framer entry (a safe strong default)
    rather than raising, so a malformed spec degrades instead of crashing a turn.
    """
    table = COUNCIL_SEAT_BACKENDS
    default_entry = table.get(posture) or table["framer"]
    override = profile.get(posture) if (profile and isinstance(profile.get(posture), dict)) else {}
    # Per-field merge so a profile seat can set only a role_prompt (keeping the
    # posture's default backend/fallback) — P4 profiles. A seat with no override is
    # byte-identical to P1; the default posture table carries no role_prompt.
    backend = override.get("backend") or default_entry["backend"]
    fallback_chain = list(override.get("fallback_chain") or default_entry.get("fallback_chain") or [])
    role_prompt = str(override.get("role_prompt") or "")
    return backend, fallback_chain, role_prompt


# ── Shared panel prompt (fusion = identical prompt across all seats) ──────────

def _build_panel_task(topic: str, context: str = "", reseed_context: str = "") -> str:
    """Build the single shared prompt every fusion seat answers blind.

    Splices the COUNCIL-OS protocol summary so each seat answers under the
    ratified protocols, exactly like the engine's per-voice prompts. Fusion
    seats all receive this identical text (no seat sees another's output).

    ``reseed_context`` (P2 grounded restart, §A2): when present, this is a
    re-seeded "round 1" — the grounding gate detected web drift and is re-running
    the parallel panel seeded with ``OG topic + output-so-far + synth steer +
    grounding research``. It is spliced in as a distinct, prominent block so the
    seats re-deliberate over the corrected information set (distinct from a fresh
    run, which carries no reseed block). Empty/absent ``reseed_context`` ⇒ the
    prompt is byte-identical to P1 (additive — existing tests stay green).
    """
    protocols = load_protocols()
    summary = protocols.get("summary", _PROTOCOLS_SUMMARY_TEMPLATE)
    parts = [f"PROTOCOLS IN EFFECT:\n{summary}", f"\nTOPIC: {topic}"]
    if context:
        parts.append(f"\nPRIOR COUNCIL CONTEXT:\n{context}")
    if reseed_context:
        parts.append(f"\n{reseed_context}")
    parts.append(
        "\nProvide your analysis as one council voice. "
        "Follow [PROT-01] (new content only), [PROT-02] (cite when challenging), "
        "[PROT-03] (state load-bearing assumptions, not confidence levels). "
        "Surface the load-bearing assumptions and the strongest objection you can "
        "find to your own position."
    )
    return "\n".join(parts)


def _build_debate_context(panel_results: list[dict], prev_round: int) -> str:
    """Format the PRIOR round's seat positions for the next debate round (D1).

    Each seat sees the others' prior positions and responds delta-only ([PROT-01]):
    advance, challenge (with citation), or concede — surfacing remaining disputes as
    Idea-IDs that the convergence gate (D2) tracks. Returns "" when there is no prior
    round (round 0) or no prior text, so the seat prompt is byte-identical to a
    fusion first round. Reads ONLY ``prev_round`` entries (panel_results accumulates
    across rounds via operator.add).
    """
    if prev_round < 0:
        return ""
    prior = sorted(
        (r for r in (panel_results or []) if r.get("round", 0) == prev_round),
        key=lambda r: r.get("idx", 0),
    )
    blocks = []
    for r in prior:
        posture = r.get("posture") or f"seat-{r.get('idx', 0)}"
        text = (r.get("text") or "").strip()
        if text:
            blocks.append(f"[{posture}] argued:\n{text}")
    if not blocks:
        return ""
    return (
        "PRIOR ROUND POSITIONS — the council is in DEBATE. Respond DELTA-ONLY "
        "([PROT-01]): advance the strongest line, challenge a specific claim (cite the "
        "seat), or concede. Do NOT restate. Name each remaining point of disagreement "
        "as an Idea-ID so it can be tracked to resolution.\n\n" + "\n\n".join(blocks)
    )


# ── Fusion nodes ─────────────────────────────────────────────────────────────

def panel_dispatch_node(state: dict) -> dict:
    """State-mutation node: resolve each seat's backend, set up the fan-out.

    Reads ``council_spec`` (``{mode, seats, synth_backend, ...}``); for each seat
    posture resolves ``(backend, fallback_chain)`` via :func:`resolve_seat_backend`
    and writes the resolved seat list onto ``council_spec["resolved_seats"]`` plus
    the shared blind prompt onto ``council_spec["panel_task"]``. ``_route_after_panel``
    consumes these. Bounded: at most ``len(seats)`` Sends — council is off the
    worker cost-cap path, so no cost pre-flight here (design "off the worker path").
    """
    spec = dict(state.get("council_spec") or {})
    # Default ONLY when seats is absent/None — an explicit empty list means
    # "no panel" (→ _route_after_panel falls through to synthesize), not "defaults".
    seats_raw = spec.get("seats")
    seats: list[str] = list(COUNCIL_DEFAULT_SEATS if seats_raw is None else seats_raw)
    topic = state.get("task", "")
    context = spec.get("context", "") or ""
    # P2: a grounded restart writes spec["reseed_context"]; splice it so this
    # "round 1" re-run is seeded with the grounded context. Absent ⇒ P1 prompt.
    reseed_context = spec.get("reseed_context", "") or ""

    # D1 (debate): from round 1 on, seats SEE the prior round's positions (the key
    # difference from fusion, whose seats answer blind). Build that context from the
    # previous round's panel_results and splice it through the SAME prominent slot
    # as a grounded restart. Round 0 (no prior results) → empty → byte-identical to
    # a fusion first round.
    mode = (spec.get("mode") or "fusion").strip().lower()
    if mode == "debate":
        prev_round = (state.get("council_round") or 0) - 1
        debate_ctx = _build_debate_context(state.get("panel_results") or [], prev_round)
        if debate_ctx:
            reseed_context = debate_ctx

    profile = spec.get("profile")  # P4 hook; None in P1b.
    resolved = []
    for idx, posture in enumerate(seats):
        backend, fallback_chain, role_prompt = resolve_seat_backend(posture, profile)
        resolved.append(
            {
                "idx": idx,
                "posture": posture,
                "backend": backend,
                "fallback_chain": fallback_chain,
                "role_prompt": role_prompt,
            }
        )

    spec["resolved_seats"] = resolved
    spec["panel_task"] = _build_panel_task(topic, context, reseed_context)
    return {"council_spec": spec}


def _route_after_panel(state: dict) -> Union[list[Send], str]:
    """Conditional edge: one ``Send("panel_worker", ...)`` per resolved seat.

    Mirrors ``_route_after_dispatch``'s replication Send. The shared ``panel_task``
    is identical across every Send (fusion = all seats answer the same prompt
    blind). Falls through to ``synthesize`` with no Sends only if the spec is
    empty (defensive — should not happen on the council branch).
    """
    spec = state.get("council_spec") or {}
    resolved = spec.get("resolved_seats") or []
    task = spec.get("panel_task", "")
    # Stamp the current round so synthesize reads ONLY the latest round's results
    # (panel_results accumulates via operator.add). DEBATE counts rounds on
    # ``council_round``; the fusion grounded-restart loop counts on
    # ``council_restart``. Both default 0 ⇒ synthesize's filter is a no-op for a
    # normal single-round run.
    mode = (spec.get("mode") or "fusion").strip().lower()
    panel_round = (state.get("council_round") if mode == "debate"
                   else state.get("council_restart")) or 0
    if not resolved:
        return "synthesize"
    return [
        Send(
            "panel_worker",
            {
                "seat_posture": seat["posture"],
                "backend": seat["backend"],
                "fallback_chain": seat["fallback_chain"],
                "role_prompt": seat.get("role_prompt", ""),
                "task": task,
                "seat_idx": seat["idx"],
                "panel_round": panel_round,
                "messages": [],
            },
        )
        for seat in resolved
    ]


async def panel_worker_node(sub_state: dict) -> dict:
    """Single-seat call (mirrors ``worker_node``): try backend, then fallbacks.

    Receives a per-seat sub-state via Send (NOT the full AgentState). Returns a
    state delta with ONE ``panel_results`` entry carrying ``idx`` so
    ``synthesize_node`` can sort deterministically. Each entry records the backend
    that actually produced the text (``backend`` may differ from the seat default
    when the fallback chain was walked).
    """
    posture = sub_state.get("seat_posture", "framer")
    primary = sub_state.get("backend", "local")
    fallback_chain = sub_state.get("fallback_chain") or []
    seat_idx = sub_state.get("seat_idx", 0)
    panel_round = sub_state.get("panel_round", 0)
    task = sub_state.get("task", "")
    role_prompt = (sub_state.get("role_prompt") or "").strip()

    # Per-seat steering layered on the shared constitution. Fusion stays blind: the
    # user task is identical across seats; only the system angle differs. Empty
    # role_prompt ⇒ byte-identical to P1.
    system = (
        f"{_COUNCIL_SYSTEM_BASE}\n\nYOUR ROLE:\n{role_prompt}"
        if role_prompt else _COUNCIL_SYSTEM_BASE
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]

    text = ""
    used_backend = primary
    last_error: Optional[Exception] = None
    for candidate in [primary, *fallback_chain]:
        try:
            text = await asyncio.wait_for(
                _send_to_backend(messages, candidate),
                timeout=WORKER_TIMEOUT_SECONDS,
            )
            used_backend = candidate
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — walk the fallback chain on any error
            last_error = exc
            logger.warning(
                "council seat %d (%s) backend %r failed: %s",
                seat_idx, posture, candidate, exc,
            )
            continue

    entry = {
        "idx": seat_idx,
        "posture": posture,
        "backend": used_backend,
        "text": text,
        # P2: the restart round this seat ran in (0 for a normal run). synthesize
        # reads only the max-round entries so a grounded restart's re-run round
        # supersedes the prior round (panel_results is operator.add-accumulated).
        "round": panel_round,
    }
    if last_error is not None and not text:
        entry["error"] = str(last_error)
    return {"panel_results": [entry]}
