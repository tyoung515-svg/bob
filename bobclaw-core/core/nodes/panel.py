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

from core.backends import _cost
from core.config import (
    COUNCIL_DEFAULT_SEATS,
    COUNCIL_SEAT_BACKENDS,
    WORKER_TIMEOUT_SECONDS,
)
from core.council.engine import BackendFn, CostFn
from core.council.events import (
    PHASE_PANEL_START,
    PHASE_SEAT_START,
    emit_council_event,
    emit_council_event_sync,
)
from core.council.protocol import _PROTOCOLS_SUMMARY_TEMPLATE, load_protocols
from core.nodes.budget_runtime import measure_spend
from core.nodes.execute import _send_to_backend
from core.telemetry.emit import KIND_COUNCIL_SEAT, emit_event
from core.telemetry.flight import resolve_flight_id

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


# COST-2 estimation basis (MS5-C1) — EXPLICIT + HONEST.
#
# The council seams expose only a COMBINED token count (the engine's
# ``CouncilVoice.tokens_used`` = ``len(prompt)//4 + len(response)//4``; a fusion
# seat's ``measure_spend(messages, text)``), never the provider's real
# per-direction ``usage`` — the send seam ``_send_to_backend`` returns a bare
# string and DROPS ``usage`` (COST-1, explicitly OUT OF SCOPE here). So every
# council $ figure below is a POST-HOC TEXT-DERIVED ESTIMATE, not a metered draw.
# Two documented approximations make the combined count priceable:
#   1. SPLIT — we split the combined count 50/50 input/output (``_COUNCIL_INPUT_
#      FRACTION``) so ``_cost.usd_for`` (which prices input vs output separately)
#      can be applied. When real ``usage`` IS threaded (future / non-council
#      callers), we use it verbatim instead of the 50/50 split.
#   2. RATE — we apply ONE proven reference rate (``_cost.usd_for``'s PAYG table,
#      the same rates ``tests/telemetry/test_spend.py`` pins) to EVERY seat,
#      regardless of vendor. Per-vendor $ maps are subscription-amortized
#      fictions (COST-3), so a single proven reference rate is the honest basis;
#      the ``name`` arg is accepted (seam contract + future differentiation) but
#      does NOT vary the rate today.
# NET: this is a rate-consistent ESTIMATE for the cost-credibility story, NOT a
# provider-metered figure — eyeball it before any published number leans on it.
_COUNCIL_INPUT_FRACTION: float = 0.5


def council_token_usd(tokens: int, usage: Optional[dict] = None) -> float:
    """USD for one council seat's I/O, via the proven ``_cost.usd_for`` rate table.

    ``usage`` (real provider token metadata, Moonshot/OpenAI-shaped) is preferred
    WHEN PRESENT — it carries the true input/cached/output split, so we price it
    verbatim. It is NOT available on the current council send seam (COST-1), so in
    practice the ``tokens`` fallback runs: a combined post-hoc token estimate,
    split 50/50 input/output (see the module note) and priced via ``usd_for``.
    Pure; never negative; ``None``/0/absent ⇒ 0.0.
    """
    if isinstance(usage, dict) and usage:
        parsed = _cost.parse_usage({"usage": usage})
        return _cost.usd_for(**parsed)
    t = int(tokens or 0)
    if t <= 0:
        return 0.0
    input_tokens = int(round(t * _COUNCIL_INPUT_FRACTION))
    output_tokens = t - input_tokens
    return _cost.usd_for(input_tokens=input_tokens, output_tokens=output_tokens)


def make_cost_fn() -> CostFn:
    """The engine's ``cost_fn`` hook: ``(model/backend name, token_count) -> usd``.

    COST-2 (MS5-C1): P1b returned ``None`` (metering skipped → per-session cost
    0.0). This now returns a REAL per-token meter that prices the seat's token
    count via :func:`council_token_usd` (the proven ``_cost.usd_for`` rate table).
    ``name`` is accepted for the seam contract but does not vary the rate — a
    single reference PAYG rate is applied to every seat (COST-3: per-vendor maps
    are amortized fictions; the stale ``claude-opus-4-6``/``gemini-2.0-flash`` map
    was rightly dropped in the port). The estimation basis is documented on the
    module note above; the figure is an ESTIMATE, not a metered draw.
    """
    def _cost_fn(name: str, tokens: int) -> float:
        return council_token_usd(tokens)

    return _cost_fn


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

    # U7 (opt-in): mark the start of this panel round so the theater can render
    # "round N began · seats=[...]" BEFORE any seat completes (the existing
    # council_seat frame fires only on completion). Sync node ⇒ emit_event_sync
    # (stream fork in-turn; Redis fork scheduled iff a loop is running — the same
    # policy as dispatch's fleet_start). NO-OP + byte-identical when opt-in absent.
    panel_round = (state.get("council_round") if mode == "debate"
                   else state.get("council_restart")) or 0
    emit_council_event_sync(
        spec, state, PHASE_PANEL_START, round_idx=panel_round,
        extra={"mode": mode, "seats": [r["posture"] for r in resolved]},
    )
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
    # L0.2: thread the resolved flight so per-seat council_seat events group under the
    # right flight (panel sub-states carry no conversation_id; resolve once here).
    flight_id = resolve_flight_id(state)
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
                "flight_id": flight_id,
                # U7 (opt-in): thread the emit gate onto the per-seat Send sub-state
                # (panel_worker gets a sub-state, NOT the full council_spec). Inert
                # bool; panel_worker ignores it when falsy ⇒ byte-identical.
                "emit_events": bool(spec.get("emit_events")),
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

    # U7 (opt-in): announce this seat is ABOUT to speak — the theater's "who's
    # speaking now" signal (the existing council_seat frame below fires only on
    # completion). sub_state carries the threaded emit_events gate; NO-OP + byte-
    # identical when opt-in absent.
    await emit_council_event(
        sub_state, sub_state, PHASE_SEAT_START,
        round_idx=panel_round, seat=seat_idx, posture=posture,
        extra={"backend": primary},
    )

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

    # COST-2 (MS5-C1): a post-hoc token estimate of this seat's REAL I/O (request
    # messages + response text). No provider `usage` on the send seam (COST-1), so
    # this is the text-derived basis synthesize_node sums into the real council_cost_
    # usd meter (see council_token_usd). Fail-soft to 0 — never break a seat on a
    # metering estimate.
    _seat_tokens = 0
    try:
        _seat_tokens = measure_spend(messages, text or "", None)
    except Exception:  # noqa: BLE001
        _seat_tokens = 0
    _seat_cost_usd = council_token_usd(_seat_tokens)

    entry = {
        "idx": seat_idx,
        "posture": posture,
        "backend": used_backend,
        "text": text,
        # P2: the restart round this seat ran in (0 for a normal run). synthesize
        # reads only the max-round entries so a grounded restart's re-run round
        # supersedes the prior round (panel_results is operator.add-accumulated).
        "round": panel_round,
        # COST-2: per-seat token estimate + its USD at the reference rate table.
        # synthesize_node sums the latest round's cost_usd into council_cost_usd.
        "tokens": _seat_tokens,
        "cost_usd": _seat_cost_usd,
    }
    if last_error is not None and not text:
        entry["error"] = str(last_error)
    # L0.2: emit this seat's completion (council per-seat, per KICKOFF §6 L0.2).
    await emit_event(
        KIND_COUNCIL_SEAT, resolve_flight_id(sub_state),
        {"idx": seat_idx, "posture": posture, "backend": used_backend,
         "round": panel_round, "status": "ok" if text else "failed", "tokens": _seat_tokens},
    )
    return {"panel_results": [entry]}
