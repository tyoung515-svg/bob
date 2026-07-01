package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class Idea(
    val id: String,
    @SerialName("user_id") val userId: String,
    val body: String,
    val tags: List<String> = emptyList(),
    val state: String,
    @SerialName("promoted_to") val promotedTo: String? = null,
    @SerialName("created_at") val createdAt: String,
    @SerialName("updated_at") val updatedAt: String,
)
