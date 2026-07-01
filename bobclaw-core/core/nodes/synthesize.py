"""
BoBClaw — CoCouncil rotating Synthesizer (P1b, fusion shape).

``synthesize_node`` closes the fusion path: it reads the parallel panel's
``panel_results`` (sorted by idx for determinism), builds the synthesis prompt
per [ROLE-01] (reusing the engine's ``_format_synthesis_prompt`` contract),
calls the synth backend via Bob's ``_send_to_backend`` seam, parses the
``### 📋 COUNCIL HANDOFF`` block with the engine's ``_extract_handoff``, and
emits the reconciled answer.

The engine's prompt-builder / handoff-parser are reused (NOT reinvented) by
holding a parser-only ``CouncilEngine`` instance — fusion has no Claude→Gemini
chain to run here, so the engine's backends are never called; we only borrow its
``_format_synthesis_prompt`` (which takes the voices as args) and
``_extract_handoff`` (pure). This keeps the handoff contract single-sourced.

⚠ Streaming-drop concern (the 4f7d8f4 bug): the "updates" SSE relay in
``api/server.py`` SKIPS re-emitting ``execute``'s assistant message (it assumes
token-streaming on the "custom" channel). claude_code surfaces a whole-block
reply by emitting it as a message-level "custom" chunk via
``get_stream_writer()`` AND keeping it in ``messages`` for state. We do the
SAME here, and the server's per-node suppression was extended to skip
``synthesize`` / ``council`` (alongside ``execute``) so the answer is delivered
exactly once via "custom" rather than double-emitted by also being relayed off
"updates". The writer is None in unit tests / non-streaming runs, where the
assistant message in ``messages`` is the only carrier — that's fine because
those runs don't go through the SSE relay. The live E2E must confirm exactly one
copy reaches the client.
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from core.config import COUNCIL_SEAT_BACKENDS
from core.council.engine import CouncilEngine
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.execute import _get_stream_writer, _send_to_backend
from core.nodes.panel import _COUNCIL_SYSTEM_BASE

logger = logging.getLogger(__name__)


# Parser/formatter-only engine: backends are never invoked from here (fusion's
# voices already ran in panel_worker). We only borrow _format_synthesis_prompt
# and _extract_handoff so the COUNCIL HANDOFF contract stays single-sourced.
async def _unused_backend(_system: str, _user: str) -> str:  # pragma: no cover
    raise RuntimeError("council synthesize: engine backend must not be called")


_PARSER_ENGINE = CouncilEngine(
    claude_backend=_unused_backend,
    gemini_backend=_unused_backend,
    local_backend=None,
)


def _should_defer(spec: "dict | None" = None) -> bool:
    """Defer the in-node commit to a downstream chokepoint that WILL commit:
    grounding ON (``grounding_node`` commits at converge) OR debate mode
    (``debate_converge_node`` commits at converge). When NEITHER holds, synthesize
    is the sole emitter and commits IN-NODE.

    SINGLE source read by ``synthesize_node``; the downstream gate (ground XOR
    debate_converge, selected per turn) is the SOLE emitter. If this and the gate
    disagree the final answer double-emits or drops (the streaming-drop class
    flagged above). ``grounding_enabled`` (a profile's ``protocol_bounds.grounding``
    over the global cadence) and ``is_debate`` (``mode == "debate"``) are both
    re-read at call time."""
    from core.nodes.debate import is_debate
    from core.nodes.grounding import grounding_enabled
    return grounding_enabled(spec) or is_debate(spec)


async def emit_synthesis(state: dict, content: str, backend) -> list[dict]:
    """Single-sourced final-answer COMMIT: emit the whole answer as one
    message-level "custom" chunk (so the SSE relay surfaces it exactly once),
    fire the L0 agent-turn event, and return the assistant `messages` fragment
    to append to state. Used by synthesize_node (grounding OFF) and grounding_node
    (grounding ON, on every converge path)."""
    if content:
        writer = _get_stream_writer()
        if writer is not None:
            try:
                writer(
                    {
                        "type": "token",
                        "content": content,
                        "backend": backend,
                        "model": None,
                    }
                )
            except Exception:
                logger.debug("council synth stream writer raised; continuing", exc_info=True)
    await _append_agent_turn_event(state, assistant_response=content)
    return [{"role": "assistant", "content": content}]


async def synthesize_node(state: "dict") -> dict:
    """Reconcile the fusion panel into one answer + handoff (rotating [ROLE-01]).

    Reads ``panel_results`` (sorted by idx), formats the synthesis prompt, calls
    the synth backend, parses the handoff, emits the answer WS-safely, and sets
    ``council_handoff``.
    """
    spec = state.get("council_spec") or {}
    topic = state.get("task", "")
    synth_backend = spec.get("synth_backend") or "minimax"

    # P2: after a grounded restart, panel_results accumulates BOTH the prior
    # round's entries and the re-run round's (the reducer is operator.add). Read
    # only the latest round (max ``round``, default 0) so the re-seeded round
    # supersedes the drifted one. For a normal single-round run every entry is
    # round 0, so this is a no-op (P1 behaviour — existing tests stay green).
    all_results = state.get("panel_results") or []
    latest_round = max((r.get("round", 0) for r in all_results), default=0)
    results = sorted(
        (r for r in all_results if r.get("round", 0) == latest_round),
        key=lambda r: r.get("idx", 0),
    )

    # Seats whose entire backend chain failed come back with empty text (+ an
    # "error" key from panel_worker). Track them so the answer carries a visible
    # "ran with N of M voices" signal instead of silently dropping a voice.
    degraded_seats = [
        (r.get("posture") or f"seat-{r.get('idx', 0)}")
        for r in results
        if not (r.get("text") or "").strip()
    ]

    protocols = _PARSER_ENGINE.load_protocols()

    # Fold the panel into the engine's two-voice synthesis contract: it expects
    # a "claude" voice and a "gemini" voice block. With N seats we label each
    # voice by its posture and concatenate so the synthesizer sees every seat.
    def _voice_block(seat: dict) -> str:
        label = seat.get("posture") or f"seat-{seat.get('idx', 0)}"
        body = (seat.get("text") or "").strip()
        if not body:
            # Seat whose whole backend chain failed (panel_worker returns text=""
            # + an "error" key). Tell the synthesizer explicitly instead of folding
            # in a blank voice it would silently reconcile over.
            body = "(seat unavailable — backend chain failed)"
        return f"[{label}]:\n{body}"

    if results:
        first_block = _voice_block(results[0])
        rest_block = "\n\n".join(_voice_block(r) for r in results[1:]) or "(no further seats)"
    else:
        first_block = "(no panel output)"
        rest_block = "(no panel output)"

    synth_prompt = _PARSER_ENGINE._format_synthesis_prompt(
        topic, first_block, rest_block, protocols
    )

    # Synth backend + its table-E fallback chain. One synth backend timing out or
    # failing must NOT sink the whole council (minimax timed out live 2026-06-19 →
    # silent empty bubble), so walk the fallbacks exactly like panel_worker does.
    _synth_fallbacks = [
        b for b in (COUNCIL_SEAT_BACKENDS.get("synth", {}).get("fallback_chain") or [])
        if b and b != synth_backend
    ]
    synth_candidates = [synth_backend, *_synth_fallbacks]
    synth_messages = [
        {"role": "system", "content": _COUNCIL_SYSTEM_BASE},
        {"role": "user", "content": synth_prompt},
    ]
    synthesis = ""
    used_synth = synth_backend
    _last_exc = None
    for _cand in synth_candidates:
        try:
            synthesis = await _send_to_backend(synth_messages, _cand)
            used_synth = _cand
            _last_exc = None
            if synthesis:
                break
        except Exception as exc:  # noqa: BLE001 — walk the fallback chain on any error
            _last_exc = exc
            logger.warning(
                "council synth backend %r failed: %s; trying next fallback", _cand, exc
            )
            continue

    if not synthesis:
        # Every synth backend failed/empty. Surface the error to the CLIENT via a
        # message-level "custom" chunk — NOT just `messages`, which api/server.py's
        # per-node relay SUPPRESSES for synthesize (else it's a silent empty bubble,
        # exactly the 2026-06-19 minimax-timeout symptom).
        err = (
            f"Council synthesis failed on all synth backends {synth_candidates}: "
            + (f"{type(_last_exc).__name__}: {_last_exc}" if _last_exc else "empty response")
        )
        logger.warning(err)
        _w = _get_stream_writer()
        if _w is not None:
            try:
                _w({"type": "token", "content": err, "backend": used_synth, "model": None})
            except Exception:
                logger.debug("council synth error stream-writer raised", exc_info=True)
        await _append_agent_turn_event(
            state, assistant_response=err, error_msg=str(_last_exc or "empty")
        )
        return {
            "messages": [{"role": "assistant", "content": err}],
            "error": str(_last_exc) if _last_exc else "empty synth response",
            "council_handoff": None,
            "council_pending_answer": None,
        }

    handoff = _PARSER_ENGINE._extract_handoff(synthesis)

    if degraded_seats:
        # Append a visible degradation note so a partial council never reads as a
        # full one. Done AFTER handoff extraction so it isn't parsed as a handoff
        # field; rides into both the grounding-on (stashed) and grounding-off
        # (committed) paths below.
        n_total = len(results)
        n_ran = n_total - len(degraded_seats)
        synthesis += (
            f"\n\n_⚠ Council ran with {n_ran} of {n_total} voices; "
            f"unavailable: {', '.join(degraded_seats)}._"
        )

    if _should_defer(spec):
        # ── DEFER: the downstream gate (grounding_node OR debate_converge_node)
        # owns the single converge-time commit. ──
        # Do NOT emit, append, or fire L0 here — an intermediate (restarted)
        # round must never surface/persist. Stash the answer (+ its backend, the
        # only place used_synth is preserved into state) for grounding_node to
        # commit on converge. council_pending_answer is last-write-wins, so a
        # re-run round overwrites the drifted round's value.
        return {
            "council_pending_answer": {"content": synthesis, "backend": used_synth},
            "council_handoff": asdict(handoff),
            "approval_required": False,
            "approval_response": None,
            "error": None,
        }

    # ── Grounding OFF (synthesize -> END, P1 topology) — commit in-node. ──────
    messages = await emit_synthesis(state, synthesis, used_synth)
    return {
        "messages": messages,
        "council_handoff": asdict(handoff),
        "approval_required": False,
        "approval_response": None,
        "error": None,
        "council_pending_answer": None,
    }
