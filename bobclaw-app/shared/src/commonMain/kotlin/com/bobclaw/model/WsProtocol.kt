package com.bobclaw.model

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * The screen the Ask-Bob helper bubble (U5) is asking about: page name + a serialized snapshot of
 * the visible state. Rides the EXISTING chat `message` frame as an additive `page_context` field
 * (below); the gateway forwards it and core splices it as a front-adjacent system card, flag-gated.
 * Only the bubble ever sets it — the main chat sends `message` frames without it (byte-identical).
 */
@Serializable
data class PageContext(
    val page: String,
    val snapshot: String? = null,
)

@Serializable
sealed class ClientMessage {
    @OptIn(ExperimentalSerializationApi::class)
    @Serializable
    @SerialName("message")
    data class ChatMessage(
        @SerialName("conversation_id") val conversationId: String,
        val content: String,
        @SerialName("face_id") val faceId: String? = null,
        val locale: String? = null,
        // U5: present ONLY on a helper-bubble turn. @EncodeDefault(NEVER) keeps every ordinary chat
        // `message` frame byte-identical on the wire (the field is simply absent when null).
        @EncodeDefault(EncodeDefault.Mode.NEVER)
        @SerialName("page_context") val pageContext: PageContext? = null,
        // MS9-W1: the Council theater's live opt-in. TRUE only on a council launch (below) ⇒ the
        // gateway forwards it, core stamps council_spec["emit_events"], and the seat/round frames
        // stream back to the Live view. @EncodeDefault(NEVER) ⇒ absent (false) on every ordinary
        // chat `message` frame ⇒ byte-identical on the wire (the main chat never sets it).
        @EncodeDefault(EncodeDefault.Mode.NEVER)
        @SerialName("emit_events") val emitEvents: Boolean = false
    ) : ClientMessage() {
        companion object {
            const val TYPE = "message"
        }
    }

    @Serializable
    @SerialName("switch_face")
    data class SwitchFace(
        @SerialName("conversation_id") val conversationId: String,
        @SerialName("face_id") val faceId: String,
        @SerialName("face_name") val faceName: String? = null
    ) : ClientMessage() {
        companion object {
            const val TYPE = "switch_face"
        }
    }

    @Serializable
    @SerialName("switch_model")
    data class SwitchModel(
        @SerialName("conversation_id") val conversationId: String,
        val model: String,
        val backend: String
    ) : ClientMessage() {
        companion object {
            const val TYPE = "switch_model"
        }
    }

    @Serializable
    @SerialName("switch_profile")
    data class SwitchProfile(
        @SerialName("conversation_id") val conversationId: String,
        val profile: String,
    ) : ClientMessage() {
        companion object {
            const val TYPE = "switch_profile"
        }
    }

    @Serializable
    @SerialName("switch_locale")
    data class SwitchLocale(
        @SerialName("conversation_id") val conversationId: String,
        val locale: String,
    ) : ClientMessage() {
        companion object {
            const val TYPE = "switch_locale"
        }
    }

    @Serializable
    @SerialName("approval_response")
    data class ApprovalResponse(
        @SerialName("approval_id") val approvalId: String,
        val decision: String,
        @SerialName("edit_content") val editContent: String? = null
    ) : ClientMessage() {
        companion object {
            const val TYPE = "approval_response"
        }
    }

    @Serializable
    @SerialName("stop_generation")
    data class StopGeneration(
        @SerialName("conversation_id") val conversationId: String? = null
    ) : ClientMessage() {
        companion object {
            const val TYPE = "stop_generation"
        }
    }
}

@Serializable
sealed class ServerMessage {
    @Serializable
    @SerialName("chunk")
    data class Chunk(
        val content: String,
        val model: String? = null,
        val backend: String? = null
    ) : ServerMessage() {
        companion object {
            const val TYPE = "chunk"
        }
    }

    @Serializable
    @SerialName("message_complete")
    data class MessageComplete(
        @SerialName("message_id") val messageId: String? = null,
        @SerialName("tokens_in") val tokensIn: Int = 0,
        @SerialName("tokens_out") val tokensOut: Int = 0,
        @SerialName("elapsed_ms") val elapsedMs: Int = 0
    ) : ServerMessage() {
        companion object {
            const val TYPE = "message_complete"
        }
    }

    @Serializable
    @SerialName("approval_request")
    data class ApprovalRequest(
        @SerialName("approval_id") val approvalId: String,
        val action: String,
        val details: JsonElement? = null
    ) : ServerMessage() {
        companion object {
            const val TYPE = "approval_request"
        }
    }

    @Serializable
    @SerialName("face_switched")
    data class FaceSwitched(
        @SerialName("face_id") val faceId: String? = null,
        @SerialName("face_name") val faceName: String? = null
    ) : ServerMessage() {
        companion object {
            const val TYPE = "face_switched"
        }
    }

    @Serializable
    @SerialName("model_switched")
    data class ModelSwitched(
        val model: String? = null,
        val backend: String? = null
    ) : ServerMessage() {
        companion object {
            const val TYPE = "model_switched"
        }
    }

    @Serializable
    @SerialName("profile_switched")
    data class ProfileSwitched(
        val profile: String? = null
    ) : ServerMessage() {
        companion object {
            const val TYPE = "profile_switched"
        }
    }

    @Serializable
    @SerialName("generation_stopped")
    data class GenerationStopped(
        val code: String
    ) : ServerMessage() {
        companion object {
            const val TYPE = "generation_stopped"
        }
    }

    @Serializable
    @SerialName("error")
    data class Error(
        val code: String,
        val message: String
    ) : ServerMessage() {
        companion object {
            const val TYPE = "error"
        }
    }

    // ── U8 Council theater: additive parsing of the U7 council telemetry frames ────────────
    // These ride the SAME stream (core.telemetry.emit → flat frame, top-level `type`). Adding
    // them as sealed subclasses only means the three council_* `type`s now DECODE to a value
    // instead of falling into the WS client's decode_error branch; the chat frames above are
    // untouched (byte-identical) and a genuinely unknown `type` still fails to decode (the
    // BoBClawWebSocket runCatching → ServerMessage.Error tolerance is preserved). Every extra
    // wire key (flight_id / ts / …) is dropped by the client's ignoreUnknownKeys=true.
    //
    // NOTE (U8 verify #1): today the gateway chat WS relay forwards ONLY {chunk,
    // approval_request, error, message_complete}, so these frames do NOT reach the app over the
    // live chat WS — the Live theater is proven against a MOCKED frame stream (see CouncilScreen
    // demo run). This model is the render contract + a forward-compatible parse for when the
    // U7→U8 wiring follow-up (client emit_events opt-in + relay passthrough) lands.

    /** U7 `council_event` lifecycle frame (core/council/events.py). Payload is FLAT: `phase` +
     *  `round` always; `seat`/`posture`/`backend` on seat frames; `mode`+`seats` on panel_start;
     *  `reason`+`active_debate` on converge/blocked; `next_round`+`active_debate` on advance;
     *  `cost_usd` on blocked. Optional fields default so a partial/evolving frame never breaks. */
    @Serializable
    @SerialName("council_event")
    data class CouncilEvent(
        val phase: String,
        val round: Int = 0,
        val seat: Int? = null,
        val posture: String? = null,
        val mode: String? = null,
        val backend: String? = null,
        val reason: String? = null,
        @SerialName("next_round") val nextRound: Int? = null,
        @SerialName("cost_usd") val costUsd: Double? = null,
        @SerialName("active_debate") val activeDebate: List<String> = emptyList(),
        val seats: List<String> = emptyList(),
        // MS9-W4 (fix A): the top-level reserved `flight_id` (emit.build_frame) — a stable id for THIS
        // council run. Captured so CouncilScreen folds ONLY its own run's frames (not the Ask-Bob
        // helper bubble's conversation, which shares the chat WS). Absent on chat frames.
        @SerialName("flight_id") val flightId: String? = null,
    ) : ServerMessage() {
        companion object {
            const val TYPE = "council_event"
        }
    }

    /** Pre-existing per-seat COMPLETION frame (emit.py KIND_COUNCIL_SEAT) — one per seat AFTER it
     *  answers. Additive parse so the theater can show status/tokens/backend once a seat finishes. */
    @Serializable
    @SerialName("council_seat")
    data class CouncilSeat(
        val idx: Int = 0,
        val posture: String? = null,
        val backend: String? = null,
        val round: Int = 0,
        val status: String? = null,
        val tokens: Int = 0,
        // MS9-W4 (fix A): the run's `flight_id` (see CouncilEvent above).
        @SerialName("flight_id") val flightId: String? = null,
    ) : ServerMessage() {
        companion object {
            const val TYPE = "council_seat"
        }
    }

    /** Pre-existing synthesis-commit frame (emit.py KIND_COUNCIL_SYNTH) — fires when the answer
     *  commits. Marks the theater's "synthesis committed" transition. */
    @Serializable
    @SerialName("council_synth")
    data class CouncilSynth(
        val backend: String? = null,
        val status: String? = null,
        val shape: String? = null,
        // MS9-W4 (fix A): the run's `flight_id` (see CouncilEvent above).
        @SerialName("flight_id") val flightId: String? = null,
    ) : ServerMessage() {
        companion object {
            const val TYPE = "council_synth"
        }
    }
}
