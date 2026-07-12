package com.bobclaw.ui.screens

import com.bobclaw.model.ClientMessage
import com.bobclaw.model.ServerMessage
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * MS9-W1 — live council wiring (app seam). Proves without any UI:
 *   1. the Council launch opts in — a `message` frame with `emitEvents=true` serializes with
 *      `"emit_events":true`, so the gateway forwards it and core stamps council_spec.emit_events;
 *   2. it is default-OFF ⇒ byte-identical — an ordinary chat `message` frame (emitEvents=false)
 *      omits the key on the wire entirely (@EncodeDefault(NEVER));
 *   3. the reducer consumes the REAL wire-shaped council frames the relay now forwards (decoded
 *      the same way [com.bobclaw.network.BoBClawWebSocket] decodes them) into the Live view-model.
 */
class CouncilLaunchWiringTest {

    // Mirror the WS client's encoder config (BoBClawWebSocket): encodeDefaults on, type discriminator.
    private val json = Json {
        ignoreUnknownKeys = true; isLenient = true; encodeDefaults = true; classDiscriminator = "type"
    }

    // ── (1)+(2) launch opt-in serialization ──────────────────────────────────────────────────────

    @Test
    fun council_launch_message_serializes_emit_events_true() {
        val frame = ClientMessage.ChatMessage(
            conversationId = "c1", content = "deliberate", faceId = null, emitEvents = true,
        )
        val wire = json.encodeToString(ClientMessage.serializer(), frame)
        assertTrue(wire.contains("\"emit_events\":true"), "launch frame must carry emit_events=true: $wire")
        assertTrue(wire.contains("\"type\":\"message\""))
    }

    @Test
    fun ordinary_chat_message_omits_emit_events_byte_identical() {
        val frame = ClientMessage.ChatMessage(
            conversationId = "c1", content = "hi", faceId = null,
        )
        val wire = json.encodeToString(ClientMessage.serializer(), frame)
        // Default false ⇒ the key is absent on the wire (byte-identical to the pre-W1 chat frame).
        assertFalse(wire.contains("emit_events"), "ordinary chat frame must not carry emit_events: $wire")
        // And the U5 page_context field stays absent too (only the helper bubble sets it).
        assertFalse(wire.contains("page_context"))
    }

    // ── (3) the reducer consumes the real wire-shaped stream ─────────────────────────────────────

    private fun decode(text: String): ServerMessage =
        json.decodeFromString(ServerMessage.serializer(), text)

    /** The exact frames the MS9-W1 relay now forwards on the chat WS (flat, top-level `type`, with
     *  the reserved flight_id/ts keys the client ignores) — a 2-seat panel that synthesizes. */
    private val liveWire = listOf(
        """{"type":"council_event","phase":"panel_start","round":0,"seats":["framer","stress"],"mode":"fusion","flight_id":"f","ts":"t"}""",
        """{"type":"council_event","phase":"seat_start","round":0,"seat":0,"posture":"framer","backend":"claude_api","flight_id":"f"}""",
        """{"type":"council_seat","idx":0,"posture":"framer","backend":"claude_api","round":0,"status":"ok","tokens":12}""",
        """{"type":"council_event","phase":"seat_start","round":0,"seat":1,"posture":"stress","backend":"gemini_flash","flight_id":"f"}""",
        """{"type":"council_seat","idx":1,"posture":"stress","backend":"gemini_flash","round":0,"status":"ok","tokens":9}""",
        """{"type":"chunk","content":"The council converged.","model":"minimax","backend":"minimax"}""",
        """{"type":"council_synth","backend":"minimax","status":"ok","shape":"fusion"}""",
    )

    @Test
    fun reducer_folds_the_real_relay_stream_into_the_live_view() {
        val frames = liveWire.map { decode(it) }
        val s = reduceCouncilAll(frames)
        assertEquals(2, s.seats.size)
        assertEquals("framer", s.seats[0].posture)
        assertEquals("gemini_flash", s.seats[1].backend)
        assertTrue(s.seats.all { it.status == "ok" && !it.speaking })
        assertEquals("fusion", s.mode)
        assertTrue(s.answer.contains("converged"))     // the token chunk accumulated
        assertTrue(s.synthesized)                       // council_synth committed
        assertEquals(TheaterBanner.CONVERGED, s.banner) // synth flips RUNNING → CONVERGED
    }
}
