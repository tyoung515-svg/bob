"""
BoBClaw — CoCouncil sequential shape (P1b).

``council_node`` runs the engine's NATIVE flow (no fan-out): the [P-01]
Claude→Gemini→synth chain in one node. It constructs a real
``CouncilEngine`` with Bob backends adapted via ``make_backend_fn`` (the same
seam fusion uses), runs ``run_session``, writes the session log to the
in-tree (gitignored) ``data/council-logs/`` dir, emits the synthesized answer
WS-safely (mirrors ``execute``'s claude_code message-level "custom" chunk), and
sets ``council_handoff``.

Seat → backend mapping (design table E) reuses the same selector as fusion:
framer→claude_api, stress→gemini_flash, synth→minimax (with fallbacks). The
chain uses the seat *defaults* (no per-seat fallback walk inside the engine —
fallback chains are a fusion-seat affordance; the sequential engine takes one
backend per voice). P1b only: NO Chair, NO debate, NO grounding, NO budgets.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from core.council.engine import DEFAULT_LOG_DIR, CouncilEngine
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.execute import _get_stream_writer
from core.nodes.panel import make_backend_fn, make_cost_fn, resolve_seat_backend
from core.telemetry.emit import KIND_COUNCIL_SYNTH, emit_event
from core.telemetry.flight import resolve_flight_id

logger = logging.getLogger(__name__)

# In-tree council session logs (gitignored via bobclaw-core/data/). Resolved
# relative to bobclaw-core/ (this file is core/nodes/council.py → up 2 dirs).
_LOG_DIR: Path = Path(__file__).resolve().parent.parent.parent / DEFAULT_LOG_DIR


async def council_node(state: "dict") -> dict:
    """Run the sequential 3-voice council chain and emit answer + handoff."""
    spec = state.get("council_spec") or {}
    topic = state.get("task", "")
    context = spec.get("context", "") or ""

    # Resolve the three voice backends from the seat table (defaults; profile
    # override is a P4 hook threaded on the spec).
    profile = spec.get("profile")
    framer_backend, _, _ = resolve_seat_backend("framer", profile)
    stress_backend, _, _ = resolve_seat_backend("stress", profile)
    synth_backend = spec.get("synth_backend") or resolve_seat_backend("synth", profile)[0]

    # Spawn-context (intake 2026-07-18): each sequential voice carries its seat
    # identity so a CLI-subprocess backend spawns with council framing, not the
    # ambient repo charter. Inert for non-CLI backends.
    def _seat_desc(role: str) -> dict:
        return {
            "position": "council_seat",
            "role": role,
            "template": "council_seat",
            "task": topic,
        }

    engine = CouncilEngine(
        claude_backend=make_backend_fn(framer_backend, _seat_desc("framer")),
        gemini_backend=make_backend_fn(stress_backend, _seat_desc("stress")),
        local_backend=make_backend_fn(synth_backend, _seat_desc("synth")),
        cost_fn=make_cost_fn(),
        log_dir=_LOG_DIR,
    )

    try:
        session = await engine.run_session(topic, context=context)
    except Exception as exc:  # noqa: BLE001 — fail soft to a usable error message
        logger.warning("council sequential run_session failed: %s", exc)
        err = f"Council session failed: {exc}"
        await _append_agent_turn_event(state, assistant_response=err, error_msg=str(exc))
        return {
            "messages": [{"role": "assistant", "content": err}],
            "error": str(exc),
            "council_handoff": None,
        }

    # Best-effort session log (never fail the turn on a log-write error).
    try:
        engine.save_session_log(session)
    except Exception:  # noqa: BLE001
        logger.debug("council session log write failed; continuing", exc_info=True)

    answer = session.synthesis

    # ── WS-safe emit (mirrors execute.py's claude_code message-level chunk) ───
    if answer:
        writer = _get_stream_writer()
        if writer is not None:
            try:
                writer(
                    {
                        "type": "token",
                        "content": answer,
                        "backend": synth_backend,
                        "model": None,
                    }
                )
            except Exception:
                logger.debug("council stream writer raised; continuing", exc_info=True)

    # L0.2: emit the sequential council's synthesis completion.
    await emit_event(
        KIND_COUNCIL_SYNTH, resolve_flight_id(state),
        {"backend": synth_backend, "status": "ok" if answer else "empty", "shape": "sequential"},
    )
    await _append_agent_turn_event(state, assistant_response=answer)
    # COST-2 (MS5-C1): surface a per-token cost ESTIMATE (was 0.0 in P1b, where
    # ``make_cost_fn()`` returned None). It now returns a per-token meter, so
    # ``session.total_cost_estimate`` is COMPUTED FROM THE RATE TABLE (same basis the
    # fusion path uses). This is an ESTIMATE, NOT a provider-metered draw (COST-1: the
    # send seam drops real usage) — Travis must eyeball before any published number
    # leans on it.
    #
    # NO cross-node double-count: council_node (sequential shape) and synthesize_node
    # (fusion/debate shape) are MUTUALLY-EXCLUSIVE routing arms (graph
    # `_route_after_recall`: mode=="sequential" → council; else → panel_dispatch →
    # synthesize). synthesize NEVER runs on this arm, so it contributes nothing to
    # council_cost_usd here. This is the SOLE cost writer for a sequential turn — we
    # set the full session estimate (no prior to accumulate; the field starts unset on
    # a fresh council turn).
    return {
        "messages": [{"role": "assistant", "content": answer}],
        "council_handoff": asdict(session.handoff),
        "council_cost_usd": round(session.total_cost_estimate or 0.0, 6),
        "approval_required": False,
        "approval_response": None,
        "error": None,
    }
