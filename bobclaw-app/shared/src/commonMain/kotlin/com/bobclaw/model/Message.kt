package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

@Serializable
data class Message(
    val id: String,
    @SerialName("conversation_id") val conversationId: String,
    val role: String,
    val content: String,
    // Gateway serializes metadata as a JSON-encoded STRING (not a nested object) — e.g.
    // "metadata": "{\"tokens_in\": 11}". JsonElement accepts a string OR an object so decode
    // never throws; the UI ignores it for MVP.
    val metadata: JsonElement? = null,
    @SerialName("created_at") val createdAt: String
)
