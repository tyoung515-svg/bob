package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class TokenPair(
    @SerialName("access_token") val access: String,
    @SerialName("refresh_token") val refresh: String,
    // No gateway counterpart — kept nullable+default null; no proactive-expiry logic should rely on them.
    val expiresInSeconds: Long? = null,
    val expiresAtEpochSeconds: Long? = null
)
