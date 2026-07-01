package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable
data class ApprovalItem(
    val id: String,
    @SerialName("conversation_id") val conversationId: String? = null,
    @SerialName("user_id") val userId: String,
    @SerialName("action_type") val actionType: String,
    val details: JsonObject? = null,
    val status: String,
    @SerialName("decided_at") val decidedAt: String? = null,
    @SerialName("created_at") val createdAt: String,
)
