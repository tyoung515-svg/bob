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
    @SerialName("ui_theme") val uiTheme: String,
    // U2 display metadata (SPEC §6 / D10), passed through by GET /faces (FaceSummary) and
    // GET /capabilities faces[]. All optional + default-null: an older gateway that omits them
    // ⇒ null ⇒ the UI falls back to [id]/[name]/today's label (zero behavior change). U9 reads
    // [displayName]/[blurb] for friendly Simple-mode copy and [simpleSlot] to drive the Simple
    // mode picker (Quick / Think hard / Team of experts) with NO hardcoded app-side face map.
    @SerialName("display_name") val displayName: String? = null,
    val blurb: String? = null,
    @SerialName("simple_slot") val simpleSlot: String? = null,
)
