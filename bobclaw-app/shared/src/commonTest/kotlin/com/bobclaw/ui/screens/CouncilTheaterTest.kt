package com.bobclaw.ui.screens

import com.bobclaw.model.Message
import com.bobclaw.model.ServerMessage
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * U8 Council theater — pure-logic gate (SPEC-UI-OVERHAUL §5). Proves without any UI:
 *   1. **WsProtocol** additively parses the U7 `council_event` frame + the pre-existing
 *      `council_seat`/`council_synth` frames; the chat path stays byte-identical; a genuinely
 *      unknown `type` still fails to decode (the client's decode_error tolerance is preserved).
 *   2. The **Live reducer** folds an ordered mock frame stream into the right theater view-model
 *      (seats, who's-speaking, round, convergence board, cost ticker, banner).
 *   3. The **Replay parser** reconstructs a run from a persisted COUNCIL HANDOFF blob.
 */
class CouncilTheaterTest {

    // Mirror the WS client's decoder config (BoBClawWebSocket): ignore extra keys (flight_id/ts),
    // lenient, type discriminator "type".
    private val json = Json {
        ignoreUnknownKeys = true; isLenient = true; classDiscriminator = "type"
    }

    private fun decode(text: String): ServerMessage =
        json.decodeFromString(ServerMessage.serializer(), text)

    // ── (1) WsProtocol additive parse ──────────────────────────────────────────────────────────

    @Test
    fun council_event_frame_decodes_with_flat_payload_and_ignored_reserved_keys() {
        val wire = """
            {"phase":"seat_start","round":1,"seat":2,"posture":"skeptic","backend":"minimax",
             "type":"council_event","flight_id":"abc","ts":"2026-07-08T00:00:00Z"}
        """.trimIndent()
        val msg = decode(wire)
        assertTrue(msg is ServerMessage.CouncilEvent)
        msg as ServerMessage.CouncilEvent
        assertEquals("seat_start", msg.phase)
        assertEquals(1, msg.round)
        assertEquals(2, msg.seat)
        assertEquals("skeptic", msg.posture)
        assertEquals("minimax", msg.backend)
    }

    @Test
    fun council_event_converge_frame_carries_reason_and_active_debate() {
        val wire = """{"phase":"round_converged","round":1,"reason":"no active debate",
            "active_debate":["IDEA-02"],"cost_usd":0.06,"type":"council_event","flight_id":"f"}"""
        val msg = decode(wire) as ServerMessage.CouncilEvent
        assertEquals("no active debate", msg.reason)
        assertEquals(listOf("IDEA-02"), msg.activeDebate)
        assertEquals(0.06, msg.costUsd)
    }

    @Test
    fun council_seat_and_synth_frames_decode() {
        val seat = decode("""{"idx":0,"posture":"proposer","backend":"minimax","round":0,
            "status":"ok","tokens":512,"type":"council_seat"}""") as ServerMessage.CouncilSeat
        assertEquals(0, seat.idx)
        assertEquals("ok", seat.status)
        assertEquals(512, seat.tokens)
        val synth = decode("""{"backend":"minimax","status":"ok","shape":"debate","type":"council_synth"}""")
        assertTrue(synth is ServerMessage.CouncilSynth)
    }

    @Test
    fun chat_path_is_byte_identical_chunk_still_decodes() {
        // Additive council parsing must not disturb the existing chat frames.
        val chunk = decode("""{"content":"hi","model":"m","backend":"b","type":"chunk"}""")
        assertTrue(chunk is ServerMessage.Chunk)
        assertEquals("hi", (chunk as ServerMessage.Chunk).content)
        val complete = decode("""{"message_id":"x","tokens_in":1,"tokens_out":2,"type":"message_complete"}""")
        assertTrue(complete is ServerMessage.MessageComplete)
    }

    @Test
    fun a_genuinely_unknown_type_still_fails_to_decode_preserving_client_tolerance() {
        // The BoBClawWebSocket wraps decode in runCatching → ServerMessage.Error(decode_error);
        // that tolerance depends on an unknown discriminator THROWING. Prove it still throws.
        assertFailsWith<SerializationException> {
            decode("""{"idx":0,"role":"worker","type":"worker_state"}""")
        }
    }

    // ── (2) Live reducer ───────────────────────────────────────────────────────────────────────

    @Test
    fun mock_run_reduces_to_a_converged_two_seat_two_round_debate() {
        val s = reduceCouncilAll(mockCouncilRun())
        assertEquals(TheaterBanner.CONVERGED, s.banner)
        assertEquals(1, s.round)
        assertEquals("debate", s.mode)
        assertEquals(2, s.seats.size)
        assertTrue(s.seats.all { it.status == "ok" && !it.speaking })
        assertNull(s.currentSpeaker)
        // convergence board: both ideas resolved after converge
        assertEquals(mapOf("IDEA-01" to IdeaStatus.RESOLVED, "IDEA-02" to IdeaStatus.RESOLVED), s.convergence)
        assertEquals(listOf("IDEA-01", "IDEA-02"), s.resolvedIdeas)
        assertTrue(s.activeIdeas.isEmpty())
        assertEquals(0.06, s.costUsd)          // latest cost_usd frame wins
        assertTrue(s.answer.isNotEmpty())      // token chunk accumulated
        assertTrue(s.synthesized)
    }

    @Test
    fun seat_start_marks_who_is_speaking_before_the_completion_frame_clears_it() {
        val afterStart = reduceCouncilAll(
            listOf(
                ServerMessage.CouncilEvent(phase = PHASE_PANEL_START, round = 0, seats = listOf("a", "b")),
                ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, round = 0, seat = 1, posture = "b", backend = "x"),
            )
        )
        assertEquals(1, afterStart.currentSpeaker)
        assertTrue(afterStart.seats.first { it.seat == 1 }.speaking)
        assertFalse(afterStart.seats.first { it.seat == 0 }.speaking)
        // completion clears speaking + currentSpeaker
        val afterDone = reduceCouncil(
            afterStart,
            ServerMessage.CouncilSeat(idx = 1, posture = "b", backend = "x", round = 0, status = "ok", tokens = 10),
        )
        assertNull(afterDone.currentSpeaker)
        assertFalse(afterDone.seats.first { it.seat == 1 }.speaking)
        assertEquals("ok", afterDone.seats.first { it.seat == 1 }.status)
    }

    @Test
    fun round_advanced_resolves_ideas_that_drop_off_active_debate() {
        val s = reduceCouncilAll(
            listOf(
                ServerMessage.CouncilEvent(phase = PHASE_ROUND_ADVANCED, round = 0, nextRound = 1,
                    activeDebate = listOf("A", "B", "C")),
                ServerMessage.CouncilEvent(phase = PHASE_ROUND_ADVANCED, round = 1, nextRound = 2,
                    activeDebate = listOf("B")),
            )
        )
        assertEquals(IdeaStatus.RESOLVED, s.convergence["A"])
        assertEquals(IdeaStatus.ACTIVE, s.convergence["B"])
        assertEquals(IdeaStatus.RESOLVED, s.convergence["C"])
        assertEquals(2, s.round)
        assertEquals(TheaterBanner.RUNNING, s.banner)
    }

    @Test
    fun blocked_frame_sets_banner_and_keeps_open_ideas_active_with_cost() {
        val s = reduceCouncilAll(
            listOf(
                ServerMessage.CouncilEvent(phase = PHASE_ROUND_ADVANCED, round = 0, nextRound = 1,
                    activeDebate = listOf("IDEA-09", "IDEA-10")),
                ServerMessage.CouncilEvent(phase = PHASE_BLOCKED, round = 1, reason = "cost_ceiling",
                    costUsd = 0.5, activeDebate = listOf("IDEA-09")),
            )
        )
        assertEquals(TheaterBanner.BLOCKED, s.banner)
        assertEquals("cost_ceiling", s.reason)
        assertEquals(0.5, s.costUsd)
        assertEquals(IdeaStatus.ACTIVE, s.convergence["IDEA-09"])   // still open (blocked)
        assertEquals(IdeaStatus.RESOLVED, s.convergence["IDEA-10"]) // dropped off
    }

    @Test
    fun council_synth_never_overrides_a_blocked_banner() {
        val blocked = reduceCouncil(
            CouncilTheaterState(),
            ServerMessage.CouncilEvent(phase = PHASE_BLOCKED, round = 0, reason = "cost_ceiling", costUsd = 0.9),
        )
        assertEquals(TheaterBanner.BLOCKED, blocked.banner)
        val afterSynth = reduceCouncil(blocked, ServerMessage.CouncilSynth(backend = "minimax", status = "ok"))
        assertEquals(TheaterBanner.BLOCKED, afterSynth.banner) // still blocked, not flipped to converged
    }

    @Test
    fun non_council_frames_pass_through_unchanged() {
        val base = reduceCouncilAll(mockCouncilRun())
        val after = reduceCouncil(base, ServerMessage.FaceSwitched(faceId = "x", faceName = "y"))
        assertEquals(base, after)
    }

    // ── (3) Replay parser ──────────────────────────────────────────────────────────────────────

    private val HANDOFF_BLOB = """
        The council converged on the additive tap. Ship it opt-in.

        ### 📋 COUNCIL HANDOFF
        - **[RESOLVED]:** IDEA-01, IDEA-02
        - **[ACTIVE DEBATE]:** None
        - **[BLOCKED]:** None
        - **[CORRECTION]:** None
        - **[NEXT TASK]:** @Human: review the tradeoff

        _⚠ Council ran with 2 of 3 voices; unavailable: skeptic, judge._
    """.trimIndent()

    @Test
    fun replay_parses_a_converged_handoff_blob() {
        val r = parseCouncilHandoff(HANDOFF_BLOB)
        assertTrue(r.found)
        assertEquals(listOf("IDEA-01", "IDEA-02"), r.resolved)
        assertTrue(r.activeDebate.isEmpty())
        assertTrue(r.blocked.isEmpty())
        assertEquals("@Human: review the tradeoff", r.nextTask)
        assertEquals(TheaterBanner.CONVERGED, r.outcome)
        assertEquals(2, r.ranVoices)
        assertEquals(3, r.totalVoices)
        assertEquals(listOf("skeptic", "judge"), r.unavailable)
        assertTrue(r.body.startsWith("The council converged"))
    }

    @Test
    fun replay_infers_unresolved_when_active_debate_persisted() {
        val blob = """
            partial run

            ### 📋 COUNCIL HANDOFF
            - **[RESOLVED]:** IDEA-01
            - **[ACTIVE DEBATE]:** IDEA-02, IDEA-03
            - **[BLOCKED]:** None
            - **[NEXT TASK]:** keep going
        """.trimIndent()
        val r = parseCouncilHandoff(blob)
        assertEquals(listOf("IDEA-02", "IDEA-03"), r.activeDebate)
        assertEquals(TheaterBanner.RUNNING, r.outcome) // still-open ideas ⇒ unresolved
    }

    @Test
    fun replay_infers_blocked_when_handoff_lists_blockers() {
        val blob = """
            ### 📋 COUNCIL HANDOFF
            - **[RESOLVED]:** None
            - **[ACTIVE DEBATE]:** IDEA-05
            - **[BLOCKED]:** need production DB credentials
            - **[NEXT TASK]:** @Human
        """.trimIndent()
        val r = parseCouncilHandoff(blob)
        assertEquals(listOf("need production DB credentials"), r.blocked)
        assertEquals(TheaterBanner.BLOCKED, r.outcome)
    }

    @Test
    fun a_plain_chat_message_is_not_a_council_run() {
        val r = parseCouncilHandoff("Just an ordinary assistant reply, no handoff here.")
        assertFalse(r.found)
        assertTrue(r.resolved.isEmpty())
    }

    @Test
    fun find_council_run_picks_the_assistant_handoff_message() {
        val msgs = listOf(
            Message(id = "3", conversationId = "c", role = "assistant", content = "latest plain reply", metadata = null, createdAt = "t3"),
            Message(id = "2", conversationId = "c", role = "assistant", content = HANDOFF_BLOB, metadata = null, createdAt = "t2"),
            Message(id = "1", conversationId = "c", role = "user", content = "the ask", metadata = null, createdAt = "t1"),
        )
        val r = findCouncilRun(msgs)
        assertTrue(r != null && r.found)
        assertEquals(listOf("IDEA-01", "IDEA-02"), r.resolved)
    }

    @Test
    fun find_council_run_returns_null_when_no_run_present() {
        val msgs = listOf(
            Message(id = "1", conversationId = "c", role = "assistant", content = "no handoff", metadata = null, createdAt = "t1"),
        )
        assertNull(findCouncilRun(msgs))
    }

    // ── (4) MS9-W5 stall detection (finding B, app belt-and-suspenders) ──────────────────────────

    /** A fusion run where both seats have completed but NO terminal frame has arrived — the exact
     *  live shape (banner RUNNING, all seats "done", no synth). */
    private fun runningAllSeatsDone(): CouncilTheaterState =
        reduceCouncilAll(
            listOf(
                ServerMessage.CouncilEvent(phase = PHASE_PANEL_START, round = 0, mode = "fusion",
                    seats = listOf("framer", "stress")),
                ServerMessage.CouncilSeat(idx = 0, posture = "framer", backend = "claude_api",
                    round = 0, status = "ok", tokens = 120),
                ServerMessage.CouncilSeat(idx = 1, posture = "stress", backend = "gemini_flash",
                    round = 0, status = "ok", tokens = 90),
            )
        )

    @Test
    fun stalled_when_running_all_seats_done_and_quiet_past_the_window() {
        val s = runningAllSeatsDone()
        assertEquals(TheaterBanner.RUNNING, s.banner)   // no terminal frame arrived
        assertFalse(s.synthesized)
        assertTrue(councilStalled(s, msSinceLastFrame = 60_000L))  // past the 45s window
        assertFalse(councilStalled(s, msSinceLastFrame = 5_000L))  // still within the window
    }

    @Test
    fun not_stalled_once_a_terminal_outcome_lands() {
        val s = runningAllSeatsDone()
        // Converged via council_synth (the fusion terminal frame) → never stalled.
        val synthed = reduceCouncil(s, ServerMessage.CouncilSynth(backend = "minimax", status = "ok"))
        assertEquals(TheaterBanner.CONVERGED, synthed.banner)
        assertFalse(councilStalled(synthed, msSinceLastFrame = 10_000_000L))
        // Blocked banner (the degrade path's terminal frame) → never stalled either.
        val blocked = reduceCouncil(s,
            ServerMessage.CouncilEvent(phase = PHASE_BLOCKED, round = 0, reason = "synth_unavailable"))
        assertEquals(TheaterBanner.BLOCKED, blocked.banner)
        assertFalse(councilStalled(blocked, msSinceLastFrame = 10_000_000L))
    }

    @Test
    fun not_stalled_while_a_seat_speaks_or_before_any_seat_starts() {
        // A seat still speaking (mid-run) is not a stall even past the window.
        val speaking = reduceCouncilAll(
            listOf(
                ServerMessage.CouncilEvent(phase = PHASE_PANEL_START, round = 0, seats = listOf("a", "b")),
                ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, round = 0, seat = 0, posture = "a", backend = "x"),
            )
        )
        assertFalse(councilStalled(speaking, msSinceLastFrame = 60_000L))
        // No seats yet (fresh state) → not stalled.
        assertFalse(councilStalled(CouncilTheaterState(), msSinceLastFrame = 60_000L))
    }

    @Test
    fun seat_tokens_sums_reported_per_seat_tokens() {
        assertEquals(210, runningAllSeatsDone().seatTokens)  // 120 + 90
        assertEquals(0, CouncilTheaterState().seatTokens)
    }
}
