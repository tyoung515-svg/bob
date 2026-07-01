package com.bobclaw.model

import kotlinx.serialization.Serializable

@Serializable
data class ModelInfo(
    val id: String,
    val name: String,
    val backend: String,
    val isLocal: Boolean,
    val isAvailable: Boolean
)
