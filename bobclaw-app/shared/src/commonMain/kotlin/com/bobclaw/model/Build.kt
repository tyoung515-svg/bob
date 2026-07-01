package com.bobclaw.model

import kotlinx.serialization.Serializable

@Serializable
data class Build(
    val id: String,
    val task: String,
    val status: String,
    val model: String?,
    val startedAt: String?,
    val completedAt: String?
)
