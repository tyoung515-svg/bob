package com.bobclaw.ui.screens

import com.bobclaw.model.Message
import com.bobclaw.model.ServerMessage

/**
 * U8 Council theater — the PURE, Compose-free reduction layer (SPEC-UI-OVERHAUL §5).
 *
 * Two responsibilities, both fully unit-testable without any UI:
 *   1. **Live reduce** — fold the U7 `council_event` lifecycle frames (+ the pre-existing
 *      `council_seat` / `council_synth` completion frames and the `chunk` answer stream) into a
 *      [CouncilTheaterState]: seat cards, current round, who's-speaking, an Idea-ID convergence
 *      board (from the `[ACTIVE DEBATE]` handoff carried on each frame), a cost ticker, and a
 *      converged / blocked banner. See `bobclaw-core/core/council/events.py` for the frame contract.
 *   2. **Replay parse** — reconstruct a past run from a persisted conversation. Today council output
 *      lands as one giant `### 📋 COUNCIL HANDOFF` markdown blob (the final assistant message);
 *      [parseCouncilHandoff] mirrors the core engine's `_extract_handoff` to recover the outcome
 *      (resolved / active / blocked idea ids + next task + the "ran with N of M voices" degrade note).
 *
 * The reducer folds over [ServerMessage] directly, so the WsProtocol council parsing and the theater
 * render contract are exercised by the same tests.
 */

// Phase wire strings — MUST mirror core/council/events.py PHASE_* constants.
const val PHASE_PANEL_START = "panel_start"
const val PHASE_SEAT_START = "seat_start"
const val PHASE_ROUND_CONVERGED = "round_converged"
const val PHASE_ROUND_ADVANCED = "round_advanced"
const val PHASE_BLOCKED = "blocked"

/** Convergence-board status for one Idea-ID. */
enum class IdeaStatus { ACTIVE, RESOLVED }

/** The theater's headline banner. */
enum class TheaterBanner { RUNNING, CONVERGED, BLOCKED }

/** One seat card in the live panel. `speaking` = a `seat_start` fired but the completion
 *  (`council_seat`) has not yet landed; `status` is null until the seat answers. */
data class SeatView(
    val seat: Int,
    val posture: String,
    val backend: String? = null,
    val round: Int = 0,
    val status: String? = null,
    val speaking: Boolean = false,
    val tokens: Int? = null,
)

/** The reduced Live view-model. Pure fold target — [reduceCouncil] is the only writer. */
data class CouncilTheaterState(
    val phase: String? = null,
    val round: Int = 0,
    val mode: String? = null,
    val seats: List<SeatView> = emptyList(),
    val currentSpeaker: Int? = null,
    val convergence: Map<String, IdeaStatus> = emptyMap(),
    val costUsd: Double = 0.0,
    val banner: TheaterBanner = TheaterBanner.RUNNING,
    val reason: String? = null,
    val nextRound: Int? = null,
    val answer: String = "",
    val synthesized: Boolean = false,
) {
    val resolvedIdeas: List<String> get() = convergence.filterValues { it == IdeaStatus.RESOLVED }.keys.toList()
    val activeIdeas: List<String> get() = convergence.filterValues { it == IdeaStatus.ACTIVE }.keys.toList()
}

/** Roll the convergence board forward for a lifecycle frame carrying an `active_debate` set. */
private fun updateConvergence(
    prev: Map<String, IdeaStatus>,
    phase: String,
    active: List<String>,
): Map<String, IdeaStatus> {
    val activeIds = active.map { it.trim() }.filter { it.isNotEmpty() }
    val activeSet = activeIds.toSet()
    val out = LinkedHashMap(prev)
    // Track any newly-seen idea id (first-seen order preserved for stable rendering).
    for (id in activeIds) if (id !in out) out[id] = IdeaStatus.ACTIVE
    when (phase) {
        // Loop to next round: ids still on [ACTIVE DEBATE] stay ACTIVE; ones that dropped off resolved.
        PHASE_ROUND_ADVANCED -> for (k in out.keys.toList()) {
            out[k] = if (k in activeSet) IdeaStatus.ACTIVE else IdeaStatus.RESOLVED
        }
        // Debate converged (round closed → END): everything resolves.
        PHASE_ROUND_CONVERGED -> for (k in out.keys.toList()) out[k] = IdeaStatus.RESOLVED
        // Stopped on a bound: still-listed ids remain unresolved (blocked); the rest resolved.
        PHASE_BLOCKED -> for (k in out.keys.toList()) {
            out[k] = if (k in activeSet) IdeaStatus.ACTIVE else IdeaStatus.RESOLVED
        }
        else -> { /* panel_start / seat_start carry no idea ids */ }
    }
    return out
}

private fun upsertSeat(seats: List<SeatView>, seat: SeatView): List<SeatView> {
    val idx = seats.indexOfFirst { it.seat == seat.seat }
    return if (idx < 0) (seats + seat).sortedBy { it.seat }
    else seats.toMutableList().also { it[idx] = seat }
}

/**
 * Fold one [ServerMessage] into the theater state. PURE — no IO, no time, deterministic. Frames it
 * does not care about (approvals, face/model/profile switches, …) pass through unchanged, so the
 * reducer can run over the raw chat WS stream.
 */
fun reduceCouncil(state: CouncilTheaterState, msg: ServerMessage): CouncilTheaterState = when (msg) {
    is ServerMessage.CouncilEvent -> when (msg.phase) {
        PHASE_PANEL_START -> {
            val seats = if (msg.seats.isNotEmpty()) {
                msg.seats.mapIndexed { i, p -> SeatView(seat = i, posture = p, round = msg.round) }
            } else state.seats.map { it.copy(speaking = false) }
            state.copy(
                phase = msg.phase, round = msg.round, mode = msg.mode ?: state.mode,
                seats = seats, currentSpeaker = null,
                banner = if (state.banner == TheaterBanner.RUNNING) TheaterBanner.RUNNING else state.banner,
            )
        }
        PHASE_SEAT_START -> {
            val si = msg.seat ?: return state.copy(phase = msg.phase, round = msg.round)
            val existing = state.seats.firstOrNull { it.seat == si }
            val seat = (existing ?: SeatView(seat = si, posture = msg.posture ?: "seat-$si")).copy(
                posture = msg.posture ?: existing?.posture ?: "seat-$si",
                backend = msg.backend ?: existing?.backend,
                round = msg.round, speaking = true, status = null,
            )
            state.copy(
                phase = msg.phase, round = msg.round, currentSpeaker = si,
                seats = upsertSeat(state.seats.map { if (it.seat == si) it else it.copy(speaking = false) }, seat),
            )
        }
        PHASE_ROUND_ADVANCED -> state.copy(
            phase = msg.phase, round = msg.nextRound ?: (msg.round + 1),
            reason = msg.reason, nextRound = msg.nextRound,
            convergence = updateConvergence(state.convergence, msg.phase, msg.activeDebate),
            costUsd = msg.costUsd ?: state.costUsd, banner = TheaterBanner.RUNNING,
        )
        PHASE_ROUND_CONVERGED -> state.copy(
            phase = msg.phase, round = msg.round, reason = msg.reason,
            convergence = updateConvergence(state.convergence, msg.phase, msg.activeDebate),
            costUsd = msg.costUsd ?: state.costUsd, banner = TheaterBanner.CONVERGED,
            currentSpeaker = null,
        )
        PHASE_BLOCKED -> state.copy(
            phase = msg.phase, round = msg.round, reason = msg.reason,
            convergence = updateConvergence(state.convergence, msg.phase, msg.activeDebate),
            costUsd = msg.costUsd ?: state.costUsd, banner = TheaterBanner.BLOCKED,
            currentSpeaker = null,
        )
        else -> state.copy(phase = msg.phase, round = msg.round)
    }

    is ServerMessage.CouncilSeat -> {
        val existing = state.seats.firstOrNull { it.seat == msg.idx }
        val seat = (existing ?: SeatView(seat = msg.idx, posture = msg.posture ?: "seat-${msg.idx}")).copy(
            posture = msg.posture ?: existing?.posture ?: "seat-${msg.idx}",
            backend = msg.backend ?: existing?.backend, round = msg.round,
            status = msg.status ?: "ok", speaking = false, tokens = msg.tokens,
        )
        state.copy(
            seats = upsertSeat(state.seats, seat),
            currentSpeaker = if (state.currentSpeaker == msg.idx) null else state.currentSpeaker,
        )
    }

    // Synthesis committed. Marks completion for a fusion run (no round frames); never overrides a
    // BLOCKED banner (blocked → _commit still emits council_synth on the way out).
    is ServerMessage.CouncilSynth -> state.copy(
        synthesized = true, currentSpeaker = null,
        banner = if (state.banner == TheaterBanner.RUNNING) TheaterBanner.CONVERGED else state.banner,
    )

    // The final answer streams as token chunks.
    is ServerMessage.Chunk -> state.copy(answer = state.answer + msg.content)

    else -> state
}

/** Fold an ordered list of frames from an initial state — the Live/mock render path. */
fun reduceCouncilAll(
    frames: List<ServerMessage>,
    initial: CouncilTheaterState = CouncilTheaterState(),
): CouncilTheaterState = frames.fold(initial, ::reduceCouncil)

// ─────────────────────────────────────────────────────────────────────────────────────────────
// MS9-W5 (finding B) — belt-and-suspenders stall detection (app side).
// The core fix guarantees a terminal frame, so a healthy run ALWAYS resolves the banner. This is
// the honest fallback for the pathological case (a wedged service that emits no frame at all):
// instead of a frozen "Deliberating… $0.0000", the Live view surfaces "still working…" once every
// started seat has reported yet no terminal frame has advanced the run for a while.
// ─────────────────────────────────────────────────────────────────────────────────────────────

/** Default quiet window (ms) after the last folded frame before a still-RUNNING council reads as
 *  stalled. Generous — the core synth timeout (COUNCIL_SYNTH_TIMEOUT_SECONDS, 120s) fires first on
 *  a real hang; this only trips if NO frame at all arrives (a wedged transport). */
const val COUNCIL_STALL_WINDOW_MS: Long = 45_000L

/**
 * Pure stall predicate. A run is stalled when it is still [TheaterBanner.RUNNING] (no
 * converged/blocked banner and no synth committed), at least one seat exists and EVERY seat has
 * already reported (none speaking, all with a status), yet no frame has advanced it for
 * [stallWindowMs]. Returns false the instant any terminal outcome lands, so a converging council is
 * NEVER marked stalled — the happy path is unaffected. Time is injected ([msSinceLastFrame]) so the
 * predicate stays deterministic and unit-testable.
 */
fun councilStalled(
    state: CouncilTheaterState,
    msSinceLastFrame: Long,
    stallWindowMs: Long = COUNCIL_STALL_WINDOW_MS,
): Boolean {
    if (state.banner != TheaterBanner.RUNNING) return false   // converged / blocked → resolved
    if (state.synthesized) return false                       // a completing synth landed
    if (state.seats.isEmpty()) return false                   // nothing has started yet
    val allSeatsReported = state.seats.all { !it.speaking && it.status != null }
    return allSeatsReported && msSinceLastFrame >= stallWindowMs
}

/** Honest "work accrued so far" signal for the stalled state: the sum of per-seat token counts the
 *  completion frames carried (cost isn't wire-available per seat). 0 when no seat reported tokens. */
val CouncilTheaterState.seatTokens: Int get() = seats.sumOf { it.tokens ?: 0 }

// ─────────────────────────────────────────────────────────────────────────────────────────────
// Replay: parse a persisted COUNCIL HANDOFF blob back into an outcome.
// ─────────────────────────────────────────────────────────────────────────────────────────────

/** The reconstruction of a past council run from its persisted chat message. */
data class CouncilReplay(
    val found: Boolean,
    val resolved: List<String> = emptyList(),
    val activeDebate: List<String> = emptyList(),
    val blocked: List<String> = emptyList(),
    val correction: List<String> = emptyList(),
    val nextTask: String = "",
    val ranVoices: Int? = null,
    val totalVoices: Int? = null,
    val unavailable: List<String> = emptyList(),
    val body: String = "",
) {
    /** Outcome banner inferred from the handoff, mirroring the Live reducer's semantics. */
    val outcome: TheaterBanner get() = when {
        blocked.isNotEmpty() -> TheaterBanner.BLOCKED
        activeDebate.isEmpty() -> TheaterBanner.CONVERGED
        else -> TheaterBanner.RUNNING // still-open ideas persisted → unresolved
    }
}

private val HANDOFF_HEADING = Regex("""#{2,3}\s*📋?\s*COUNCIL HANDOFF""", RegexOption.IGNORE_CASE)
private val DEGRADE_NOTE = Regex(
    """Council ran with\s+(\d+)\s+of\s+(\d+)\s+voices;\s*unavailable:\s*([^_\n]*)""",
    RegexOption.IGNORE_CASE,
)

private fun handoffField(block: String, label: String): String {
    // Matches `- **[RESOLVED]:** value` (the _HANDOFF_TEMPLATE bold form) AND the plainer
    // `- [RESOLVED]: value` log form — value runs to the next `- ` bullet, a heading, or the end.
    val re = Regex(
        """\*{0,2}\[${Regex.escape(label)}\]\s*:?\s*\*{0,2}\s*(.*?)(?=\n\s*-\s|\n#|\n\s*\n|$)""",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    return re.find(block)?.groupValues?.get(1)?.trim().orEmpty()
}

private fun toIdList(text: String): List<String> {
    val t = text.trim()
    if (t.isEmpty() || t.lowercase() in setOf("none", "n/a", "-")) return emptyList()
    // Strip a parenthetical hint ("(List Idea IDs …)") an empty template might carry.
    return t.split(Regex("[,\n]+"))
        .map { it.trim().removeSurrounding("(", ")").trim() }
        .filter { it.isNotEmpty() && it.lowercase() !in setOf("none", "n/a", "-") }
}

/**
 * Parse the `### 📋 COUNCIL HANDOFF` block out of a persisted assistant message (SPEC §5 replay).
 * Returns `found=false` (empty lists) when no block is present — a plain chat message is not a run.
 * Mirrors `core/council/engine.py::_extract_handoff` field-by-field.
 */
fun parseCouncilHandoff(content: String): CouncilReplay {
    val heading = HANDOFF_HEADING.find(content)
        ?: return CouncilReplay(found = false, body = content.trim())
    val block = content.substring(heading.range.last + 1)
    val body = content.substring(0, heading.range.first).trim()
    val degrade = DEGRADE_NOTE.find(content)
    return CouncilReplay(
        found = true,
        resolved = toIdList(handoffField(block, "RESOLVED")),
        activeDebate = toIdList(handoffField(block, "ACTIVE DEBATE")),
        blocked = toIdList(handoffField(block, "BLOCKED")),
        correction = toIdList(handoffField(block, "CORRECTION")),
        nextTask = handoffField(block, "NEXT TASK"),
        ranVoices = degrade?.groupValues?.get(1)?.toIntOrNull(),
        totalVoices = degrade?.groupValues?.get(2)?.toIntOrNull(),
        unavailable = degrade?.groupValues?.get(3)?.trim()?.removeSuffix(".")?.let { toIdList(it) } ?: emptyList(),
        body = body,
    )
}

/** Pick the newest persisted message that carries a COUNCIL HANDOFF block (the run's final answer).
 *  Messages arrive newest-first from `getMessages`; scan in order and return the first hit. */
fun findCouncilRun(messages: List<Message>): CouncilReplay? =
    messages.asSequence()
        .filter { it.role == "assistant" }
        .map { parseCouncilHandoff(it.content) }
        .firstOrNull { it.found }

// ─────────────────────────────────────────────────────────────────────────────────────────────
// Mock run: a canned, ordered frame sequence for the honest mock-backend Live E2E (verify #1).
// The SAME frames drive the demo render and the reducer tests — proving the theater end-to-end.
// ─────────────────────────────────────────────────────────────────────────────────────────────

/** A deterministic 2-seat, 2-round debate that converges — the mock stream the Live view replays
 *  when no live council_event path exists (gateway relay does not forward council frames today). */
fun mockCouncilRun(): List<ServerMessage> = listOf(
    ServerMessage.CouncilEvent(phase = PHASE_PANEL_START, round = 0, mode = "debate",
        seats = listOf("proposer", "skeptic")),
    ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, round = 0, seat = 0, posture = "proposer", backend = "minimax"),
    ServerMessage.CouncilSeat(idx = 0, posture = "proposer", backend = "minimax", round = 0, status = "ok", tokens = 512),
    ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, round = 0, seat = 1, posture = "skeptic", backend = "deepseek_v4"),
    ServerMessage.CouncilSeat(idx = 1, posture = "skeptic", backend = "deepseek_v4", round = 0, status = "ok", tokens = 488),
    ServerMessage.CouncilEvent(phase = PHASE_ROUND_ADVANCED, round = 0, nextRound = 1,
        activeDebate = listOf("IDEA-01", "IDEA-02"), costUsd = 0.03),
    ServerMessage.CouncilEvent(phase = PHASE_PANEL_START, round = 1, mode = "debate",
        seats = listOf("proposer", "skeptic")),
    ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, round = 1, seat = 0, posture = "proposer", backend = "minimax"),
    ServerMessage.CouncilSeat(idx = 0, posture = "proposer", backend = "minimax", round = 1, status = "ok", tokens = 401),
    ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, round = 1, seat = 1, posture = "skeptic", backend = "deepseek_v4"),
    ServerMessage.CouncilSeat(idx = 1, posture = "skeptic", backend = "deepseek_v4", round = 1, status = "ok", tokens = 377),
    ServerMessage.CouncilEvent(phase = PHASE_ROUND_CONVERGED, round = 1, reason = "no active debate",
        activeDebate = listOf("IDEA-02"), costUsd = 0.06),
    ServerMessage.Chunk(content = "The council converged: ship the additive tap, gate emission opt-in."),
    ServerMessage.CouncilSynth(backend = "minimax", status = "ok", shape = "debate"),
)
