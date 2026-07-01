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

    engine = CouncilEngine(
        claude_backend=make_backend_fn(framer_backend),
        gemini_backend=make_backend_fn(stress_backend),
        local_backend=make_backend_fn(synth_backend),
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

    await _append_agent_turn_event(state, assistant_response=answer)
    return {
        "messages": [{"role": "assistant", "content": answer}],
        "council_handoff": asdict(session.handoff),
        "approval_required": False,
        "approval_response": None,
        "error": None,
    }
