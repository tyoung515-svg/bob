package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Project(
    val id: String,
    val name: String,
    val description: String? = null,
    val instructions: String? = null,
    @SerialName("default_face_id") val defaultFaceId: String? = null,
    @SerialName("default_backend") val defaultBackend: String? = null,
    @SerialName("is_archived") val isArchived: Boolean = false,
    @SerialName("updated_at") val updatedAt: String? = null,
)

@Serializable
data class ProjectSummary(
    val id: String,
    val name: String,
    val description: String? = null,
    @SerialName("default_face_id") val defaultFaceId: String? = null,
    @SerialName("default_backend") val defaultBackend: String? = null,
    @SerialName("conversation_count") val conversationCount: Int = 0,
    @SerialName("updated_at") val updatedAt: String? = null,
)
