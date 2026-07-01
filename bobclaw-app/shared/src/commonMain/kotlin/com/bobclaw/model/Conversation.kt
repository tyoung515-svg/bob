package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Conversation(
    val id: String,
    val title: String?,
    @SerialName("face_id") val faceId: String?,
    @SerialName("model_preference") val modelPreference: String?,
    @SerialName("last_message_preview") val lastMessagePreview: String? = null,
    @SerialName("project_id") val projectId: String? = null,
    @SerialName("backend_preference") val backendPreference: String? = null,
    @SerialName("updated_at") val updatedAt: String
)
