package com.bobclaw.ui.screens

import com.bobclaw.model.ServerMessage

/**
 * MS9-W4 (fix A) — the pure "does this WS frame belong to the council run CouncilScreen launched?"
 * predicate. The Council theater and the Ask-Bob helper bubble share ONE chat WebSocket
 * ([com.bobclaw.network.BoBClawWebSocket.incomingMessages]); without a filter the Ask-Bob
 * conversation's answer chunks fold into the council theater's ANSWER (the reported leak).
 *
 * ### Wire reality (VERIFIED against `bobclaw-gateway/routers/chat.py` + `core/telemetry/emit.py`)
 *   * `council_event` / `council_seat` / `council_synth` carry a top-level **`flight_id`**
 *     (`emit.build_frame` reserves it) — a stable id for THIS council run.
 *   * `chunk` / `message_complete` carry **no id** — the relay forwards only `content`/`model`/
 *     `backend`, so the council's own synthesis answer (`emit_synthesis` streams it as one `chunk`)
 *     and a foreign Ask-Bob `chunk` are INDISTINGUISHABLE by id.
 *
 * So the filter binds to the council FLIGHT and gates bare chunks on a LIVE window: the first
 * council_* frame opens the window (and captures the flight id); `message_complete` /
 * `generation_stopped` (the gateway's end-of-turn / supersede signals) close it. A bare `chunk`
 * folds into the answer ONLY inside that window; a chunk arriving when no council flight is live (an
 * Ask-Bob reply after or alongside the run) is dropped. A council_* frame stamped with a DIFFERENT
 * flight is dropped outright.
 *
 * PURE + Compose-free so a headless `:shared:jvmTest` guards it. NOTE: the "Play mock run" path feeds
 * [reduceCouncil] directly and does NOT pass through this filter (its frames carry no `flight_id`) —
 * the filter is applied ONLY to the LIVE shared-WS collect in [CouncilScreen].
 */
data class CouncilFilter(
    /** The `flight_id` this theater is bound to (captured from the first council frame; null until then). */
    val flightId: String? = null,
    /** Are we inside our council flight's live window? Bare answer chunks fold only when this is true. */
    val live: Boolean = false,
)

/** One filter step: the advanced [CouncilFilter] + whether the frame should be folded into the theater. */
data class CouncilFilterStep(val filter: CouncilFilter, val fold: Boolean)

/** The `flight_id` a council telemetry frame carries (chat frames carry none → null). */
fun councilFlightIdOf(msg: ServerMessage): String? = when (msg) {
    is ServerMessage.CouncilEvent -> msg.flightId
    is ServerMessage.CouncilSeat -> msg.flightId
    is ServerMessage.CouncilSynth -> msg.flightId
    else -> null
}

private fun isCouncilFrame(msg: ServerMessage): Boolean =
    msg is ServerMessage.CouncilEvent || msg is ServerMessage.CouncilSeat || msg is ServerMessage.CouncilSynth

/**
 * Advance the [filter] for one incoming [msg] and decide whether to fold it into the theater. See
 * [CouncilFilter] for the flight-binding + live-window contract.
 */
fun advanceCouncilFilter(filter: CouncilFilter, msg: ServerMessage): CouncilFilterStep = when {
    isCouncilFrame(msg) -> {
        val fid = councilFlightIdOf(msg)
        val bound = filter.flightId
        if (bound != null && fid != null && fid != bound) {
            // A DIFFERENT council flight — never fold, never touch our live window.
            CouncilFilterStep(filter, fold = false)
        } else {
            // Our flight (or the first one we see / an unstamped frame): fold + open the live window.
            CouncilFilterStep(filter.copy(flightId = bound ?: fid, live = true), fold = true)
        }
    }
    // A bare answer chunk (no id) folds into the answer ONLY inside our live council window.
    msg is ServerMessage.Chunk -> CouncilFilterStep(filter, fold = filter.live)
    // End-of-turn / supersede closes the window so a following (foreign) chunk can't leak in.
    msg is ServerMessage.MessageComplete -> CouncilFilterStep(filter.copy(live = false), fold = false)
    msg is ServerMessage.GenerationStopped -> CouncilFilterStep(filter.copy(live = false), fold = false)
    // Everything else is a reduceCouncil no-op — never fold.
    else -> CouncilFilterStep(filter, fold = false)
}
