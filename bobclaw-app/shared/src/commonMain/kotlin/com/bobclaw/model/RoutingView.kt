package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * JOAT v0 routing-view (read-only) — the live faces → roles → resolved-backends
 * map under the active (or previewed) team, as served by the gateway
 * `GET /routing-view` proxy.
 *
 * [liveProbe] is `false` in v0: the health-walk probe is a no-op, so
 * [RoutingFace.resolvedBackend] is the DECLARED team mapping, not a
 * health-checked one. Surfaced so the UI can badge it honestly.
 */
@Serializable
data class RoutingView(
    @SerialName("active_team") val activeTeam: String? = null,
    val teams: List<String> = emptyList(),
    @SerialName("live_probe") val liveProbe: Boolean = false,
    val faces: List<RoutingFace> = emptyList(),
)

@Serializable
data class RoutingFace(
    val id: String,
    val role: String? = null,
    @SerialName("preferred_backend") val preferredBackend: String = "",
    @SerialName("resolved_backend") val resolvedBackend: String = "",
    @SerialName("escalation_chain") val escalationChain: List<String> = emptyList(),
    @SerialName("tool_capable") val toolCapable: Boolean = false,
)
