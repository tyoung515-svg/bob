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

import asyncio
import logging
from dataclasses import asdict

from core.config import COUNCIL_SEAT_BACKENDS, COUNCIL_SYNTH_TIMEOUT_SECONDS
from core.council.engine import CouncilEngine
from core.council.events import PHASE_BLOCKED, emit_council_event
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.budget_runtime import measure_spend
from core.nodes.execute import _get_stream_writer, _send_to_backend
from core.nodes.panel import _COUNCIL_SYSTEM_BASE, council_token_usd
from core.telemetry.emit import KIND_COUNCIL_SYNTH, emit_event
from core.telemetry.flight import resolve_flight_id

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
    # L0.2: emit the fusion-family synth commit so a monitor sees the council's
    # synthesizer ("manager") finish. The SEQUENTIAL council (council.py) emits its
    # own council_synth; this covers fusion / grounded / debate, which all commit
    # through this single chokepoint — so no double-emit. Shape from the spec mode.
    try:
        _mode = ((state.get("council_spec") or {}).get("mode") or "fusion").strip().lower()
        await emit_event(
            KIND_COUNCIL_SYNTH, resolve_flight_id(state),
            {"backend": backend, "status": "ok" if content else "empty", "shape": _mode},
        )
    except Exception:  # noqa: BLE001 — telemetry is never load-bearing
        logger.debug("council_synth emit raised; non-fatal", exc_info=True)
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
            # MS9-W5 (finding B): BOUND the synth call like the per-seat worker call
            # (panel_worker's WORKER_TIMEOUT_SECONDS). Without this a synth backend that
            # HANGS (no exception) never returns — the fallback loop only advances on an
            # Exception, so a hang stalled the whole council with no terminal frame and
            # the app banner stuck on "Deliberating…". A trip raises asyncio.TimeoutError
            # (an Exception) → the except below walks to the next candidate. Happy path
            # (a prompt backend) is byte-identical — wait_for returns the same value.
            synthesis = await asyncio.wait_for(
                _send_to_backend(synth_messages, _cand),
                timeout=COUNCIL_SYNTH_TIMEOUT_SECONDS,
            )
            used_synth = _cand
            _last_exc = None
            if synthesis:
                break
        except Exception as exc:  # noqa: BLE001 — walk the fallback chain on any error (incl. timeout)
            _last_exc = exc
            logger.warning(
                "council synth backend %r failed: %s; trying next fallback", _cand, exc
            )
            continue

    if not synthesis:
        # ── DEGRADE, NEVER HANG (MS9-W5, finding B) ──────────────────────────────
        # Every synth backend failed / TIMED OUT / returned empty (a hang now trips
        # COUNCIL_SYNTH_TIMEOUT_SECONDS above and lands here). Historically this
        # surfaced a bare error and — critically — emitted NO terminal frame, so a
        # FUSION council's app banner stuck on "Deliberating… $0.0000" forever with
        # no completing council_synth. Two guarantees now:
        #   1. Degrade to the BEST ANSWER SO FAR — the raw panel seat positions — so
        #      the council still delivers something usable (mirrors debate_converge's
        #      cost-ceiling "best answer so far" path).
        #   2. Emit a TERMINAL frame so the app banner ALWAYS resolves: a `blocked`
        #      council_event (fusion; opt-in gated ⇒ NO-OP + byte-identical when
        #      absent) AND the completing council_synth via emit_synthesis. Debate
        #      turns get their terminal frame from debate_converge_node, so we do NOT
        #      double-emit a blocked event there.
        from core.nodes.debate import is_debate

        detail = (
            f"{type(_last_exc).__name__}: {_last_exc}" if _last_exc else "empty response"
        )
        logger.warning(
            "council synth unavailable on all backends %s (%s) — degrading to the raw "
            "seat positions and emitting a terminal frame", synth_candidates, detail,
        )
        seat_blocks = [_voice_block(r) for r in results if (r.get("text") or "").strip()]
        if seat_blocks:
            degraded = (
                f"_⚠ Council synthesis was unavailable ({detail}); showing the raw "
                "seat positions below._\n\n" + "\n\n".join(seat_blocks)
            )
        else:
            degraded = (
                f"Council synthesis failed on all synth backends {synth_candidates}: "
                f"{detail}"
            )

        # Terminal `blocked` council_event (fusion only; debate_converge owns the debate
        # terminal frame). Opt-in gated ⇒ NO-OP + byte-identical when the tap is absent.
        if not is_debate(spec):
            await emit_council_event(
                spec, state, PHASE_BLOCKED,
                round_idx=(state.get("council_restart") or 0),
                extra={"reason": "synth_unavailable", "detail": detail},
            )
        # Commit through the single chokepoint: streams the degraded answer as a
        # message-level "custom" chunk (server relays it — NOT the suppressed `messages`
        # path) AND fires the completing council_synth frame (banner resolves) + L0.
        messages = await emit_synthesis(state, degraded, used_synth)
        return {
            # Non-empty ALWAYS (a bare asyncio.TimeoutError str()s to "" — the server
            # relays out["error"] as an error frame, so it must carry a real message).
            "messages": messages,
            "error": f"council synth unavailable ({detail})",
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

    # ── COST-2 (MS5-C1): accumulate a per-token council cost ESTIMATE ────────────
    # Replaces the fusion=$0 governor guess with a rate-table figure: the latest
    # round's panel seats each carry a post-hoc cost_usd (panel_worker), and the
    # synth call's own I/O is measured here — both priced by the proven rate table
    # (panel.council_token_usd → _cost.usd_for). Runs on the fusion/grounding close
    # path (both grounding-on defer and grounding-off in-node commit). SKIPPED in
    # debate mode, where debate_converge_node owns the per-round cost estimate —
    # adding it here too would DOUBLE-COUNT the round's panel+synth (audit-flagged
    # failure mode). `results` is the latest round only (grounded-restart
    # superseding), so prior_cost carries earlier rounds forward without
    # double-counting. This is an ESTIMATE, not a metered draw (COST-1) — see
    # panel.council_token_usd; eyeball any published figure before leaning on it.
    from core.nodes.debate import is_debate
    council_cost_delta: dict = {}
    if not is_debate(spec):
        prior_cost = state.get("council_cost_usd") or 0.0
        # `results` is the LATEST-ROUND-ONLY view (max-round filter built above at
        # `latest_round`), NOT the raw operator.add-accumulated `panel_results`. So a
        # grounded restart charges ONLY the re-run round's seats here; `prior_cost`
        # already carries the earlier rounds forward — NO double-count. (Regression-
        # pinned: test_fusion_charges_only_latest_round_not_all_panel_results.)
        panel_cost = sum(float(r.get("cost_usd") or 0.0) for r in results)
        try:
            synth_tokens = measure_spend(synth_messages, synthesis or "", None)
        except Exception:  # noqa: BLE001 — never break the turn on a metering estimate
            synth_tokens = 0
        synth_cost = council_token_usd(synth_tokens)
        council_cost_delta = {
            "council_cost_usd": round(prior_cost + panel_cost + synth_cost, 6)
        }

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
            **council_cost_delta,
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
        **council_cost_delta,
    }
