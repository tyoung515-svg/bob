package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Face(
    val id: String,
    val name: String,
    val avatar: String,
    @SerialName("preferred_backend") val preferredBackend: String,
    // Summary endpoint (GET /faces) omits allowed_tools; only GET /faces/{id} includes it.
    @SerialName("allowed_tools") val allowedTools: List<String> = emptyList(),
    @SerialName("ui_theme") val uiTheme: String
)
