package com.bobclaw.model

import kotlinx.serialization.Serializable

/**
 * Per-row health view model consumed by BackendHealthTile.
 *
 * NOT the gateway wire shape. The real `/health` response is an OBJECT
 * (HealthResponse, defined in RestClient); RestClient.getHealth() deserializes
 * that object and maps its `services` map into a list of these rows so the
 * existing tile renders unchanged. Kept @Serializable for convenience only.
 */
@Serializable
data class HealthStatus(
    val name: String,
    val status: String,
    val latencyMs: Long? = null,
    val message: String? = null,
)
