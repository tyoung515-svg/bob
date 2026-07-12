package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * The live capability registry served read-only by the gateway's `GET /capabilities` (MS8-G1).
 *
 * ONE call composes the three core read surfaces (`/api/faces` + `/api/backends` +
 * `/api/models/available`) into faces + a name-merged backend list + a capabilities summary. Both
 * the TUI slash palette and the desktop chat `/` palette read this same document — build once,
 * both consume. All fields default to empty so a partial/degraded document (the endpoint returns
 * 200 with a `warnings` list on a partial core outage) still deserializes.
 */
@Serializable
data class Capabilities(
    val faces: List<Face> = emptyList(),
    val backends: List<CapabilityBackend> = emptyList(),
    // U3 (D4): the action registry section — the SAME source the chat `/` palette, the U5 Ask-Bob
    // helper bubble (tool scope), and voice all read. Empty on an older gateway / a degraded doc.
    val actions: List<Action> = emptyList(),
    val capabilities: CapabilitySummary = CapabilitySummary(),
    // Present only when a component fetch degraded server-side; empty on a clean document.
    val warnings: List<String> = emptyList(),
)

/**
 * One user-invokable action from the U3 registry (core `core/actions/registry.py`), served in the
 * `/capabilities` `actions` section. The U5 helper bubble filters these by [pageScope] and gates
 * execution on [risk] (D11: `read`/`reversible` auto · `gated` → Approvals) with the D12 guardrails.
 * Tolerant deserialize: `params_schema`/binding are kept opaque so an added field never breaks the client.
 */
@Serializable
data class Action(
    val id: String,
    val title: String,
    @SerialName("description_plain") val descriptionPlain: String = "",
    val risk: String = "gated",
    @SerialName("undo_hint") val undoHint: String? = null,
    @SerialName("page_scope") val pageScope: List<String> = emptyList(),
    @SerialName("params_schema") val paramsSchema: JsonElement? = null,
    val binding: ActionBinding? = null,
)

/** The concrete op an [Action] drives (a gateway REST route or a chat-WS control frame). Opaque
 *  to the bubble's tier logic; the caller reads it to know what to call when executing. */
@Serializable
data class ActionBinding(
    val kind: String = "",
    val method: String? = null,
    val path: String? = null,
    @SerialName("ws_type") val wsType: String? = null,
    @SerialName("fixed_params") val fixedParams: Map<String, JsonElement> = emptyMap(),
)

/** One merged backend entry: union of `/api/models/available` (availability + model) and
 *  `/api/backends` (cost caps). Optional caps are null when the backend is only in one source. */
@Serializable
data class CapabilityBackend(
    val backend: String,
    val available: Boolean = false,
    val model: String? = null,
    @SerialName("max_usd_per_worker") val maxUsdPerWorker: Double? = null,
    @SerialName("max_fanout_width") val maxFanoutWidth: Int? = null,
)

/** The capabilities summary block: roles + counts + the sorted list of available backend names. */
@Serializable
data class CapabilitySummary(
    val roles: List<String> = emptyList(),
    @SerialName("face_count") val faceCount: Int = 0,
    @SerialName("backend_count") val backendCount: Int = 0,
    @SerialName("available_backends") val availableBackends: List<String> = emptyList(),
    @SerialName("action_count") val actionCount: Int = 0,
)
