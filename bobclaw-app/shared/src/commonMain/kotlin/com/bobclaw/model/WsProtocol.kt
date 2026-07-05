package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

@Serializable
sealed class ClientMessage {
    @Serializable
    @SerialName("message")
    data class ChatMessage(
        @SerialName("conversation_id") val conversationId: String,
        val content: String,
        @SerialName("face_id") val faceId: String? = null,
        val locale: String? = null
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
}
