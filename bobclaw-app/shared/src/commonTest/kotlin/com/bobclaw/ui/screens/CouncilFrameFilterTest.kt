package com.bobclaw.ui.screens

import com.bobclaw.model.ServerMessage
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * MS9-W4 (fix A) — headless guard for [advanceCouncilFilter], the predicate that keeps the Ask-Bob
 * helper bubble's reply (which shares the chat WS) OUT of the Council theater's ANSWER. Mirrors the
 * exact fold CouncilScreen's live collect performs: filter → (fold ? reduceCouncil).
 */
class CouncilFrameFilterTest {

    /** Fold a whole stream through the filter → reduceCouncil, exactly like CouncilScreen's collect. */
    private fun runFiltered(frames: List<ServerMessage>): CouncilTheaterState {
        var filter = CouncilFilter()
        var theater = CouncilTheaterState()
        for (f in frames) {
            val step = advanceCouncilFilter(filter, f)
            filter = step.filter
            if (step.fold) theater = reduceCouncil(theater, f)
        }
        return theater
    }

    @Test
    fun mixed_stream_folds_only_council_frames_foreign_token_never_touches_answer() {
        val theater = runFiltered(
            listOf(
                ServerMessage.CouncilEvent(
                    phase = PHASE_PANEL_START, round = 0, mode = "debate",
                    seats = listOf("proposer", "skeptic"), flightId = "flight-A",
                ),
                ServerMessage.CouncilSeat(idx = 0, posture = "proposer", round = 0, status = "ok", tokens = 10, flightId = "flight-A"),
                ServerMessage.Chunk(content = "COUNCIL ANSWER."),          // inside the live window → folds
                ServerMessage.CouncilSynth(backend = "minimax", status = "ok", shape = "debate", flightId = "flight-A"),
                ServerMessage.MessageComplete(),                            // turn ends → close the window
                ServerMessage.Chunk(content = " LEAKED ASK-BOB REPLY"),     // foreign conv → must NOT fold
                ServerMessage.MessageComplete(),
            ),
        )
        assertEquals("COUNCIL ANSWER.", theater.answer)
    }

    @Test
    fun a_chunk_before_any_council_frame_is_not_folded() {
        // e.g. an Ask-Bob reply arriving while the theater is mounted but no run is live.
        val theater = runFiltered(
            listOf(
                ServerMessage.Chunk(content = "ambient ask-bob token"),
                ServerMessage.MessageComplete(),
            ),
        )
        assertEquals("", theater.answer)
    }

    @Test
    fun a_foreign_council_flight_is_ignored_after_binding() {
        var filter = CouncilFilter()
        filter = advanceCouncilFilter(
            filter, ServerMessage.CouncilEvent(phase = PHASE_PANEL_START, flightId = "flight-A"),
        ).filter
        assertEquals("flight-A", filter.flightId)
        val step = advanceCouncilFilter(
            filter, ServerMessage.CouncilSeat(idx = 1, status = "ok", flightId = "flight-B"),
        )
        assertFalse(step.fold)                       // a different flight's frame never folds
        assertEquals("flight-A", step.filter.flightId)
    }

    @Test
    fun supersede_closes_the_live_window_so_the_next_chunk_cannot_leak() {
        var filter = CouncilFilter()
        filter = advanceCouncilFilter(
            filter, ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, seat = 0, flightId = "f1"),
        ).filter
        assertTrue(filter.live)
        filter = advanceCouncilFilter(filter, ServerMessage.GenerationStopped(code = "superseded")).filter
        assertFalse(filter.live)
        assertFalse(advanceCouncilFilter(filter, ServerMessage.Chunk(content = "ask-bob")).fold)
    }

    @Test
    fun the_council_answer_chunks_inside_the_window_still_fold() {
        val theater = runFiltered(
            listOf(
                ServerMessage.CouncilEvent(phase = PHASE_SEAT_START, seat = 0, posture = "proposer", flightId = "f1"),
                ServerMessage.Chunk(content = "part one "),
                ServerMessage.Chunk(content = "part two"),
                ServerMessage.CouncilSynth(status = "ok", flightId = "f1"),
            ),
        )
        assertEquals("part one part two", theater.answer)
    }
}
