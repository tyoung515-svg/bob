package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One backend slot in a role's roster. A role binds a LIST of these (DESIGN §6.4),
 * and a slot may carry a [rolePrompt] — an editable instruction for how that spot acts
 * (the "HOW" layer). v0 routing uses the role's primary slot.
 */
@Serializable
data class TeamSlot(
    val name: String = "",
    val backend: String = "",
    @SerialName("escalation_chain") val escalationChain: List<String> = emptyList(),
    @SerialName("role_prompt") val rolePrompt: String = "",
)

/** Per-profile loop bounds (effective once P3b lands; carried on the draft + envelope now). */
@Serializable
data class ProtocolBounds(
    @SerialName("max_rounds") val maxRounds: Int? = null,
    @SerialName("max_usd") val maxUsd: Double? = null,
    val grounding: String? = null,
)

/**
 * A JOAT team / profile (built-in or custom). A team is the roster (WHO); a profile
 * adds the HOW — per-slot role prompts + an optional [shape] (fusion/sequential/debate)
 * + [protocolBounds]. Served by the gateway `/teams` (lean) and `/profiles` (full) proxies.
 */
@Serializable
data class Team(
    val name: String,
    val builtin: Boolean = false,
    val roles: Map<String, List<TeamSlot>> = emptyMap(),
    val shape: String? = null,
    @SerialName("protocol_bounds") val protocolBounds: ProtocolBounds? = null,
    // Present on `/api/profiles` envelopes that carry an unattended cron (P5). Absent (null) on
    // built-ins / unscheduled profiles. Drives the Home "scheduled fires" tile (U1/D2).
    val schedule: Schedule? = null,
)

/** One entry in the backend palette (`/backends`): a backend + its cost / width caps. */
@Serializable
data class BackendInfo(
    val backend: String,
    @SerialName("max_usd_per_worker") val maxUsdPerWorker: Double = 0.0,
    @SerialName("max_fanout_width") val maxFanoutWidth: Int? = null,
)

/** The builder palette: selectable backends + the apex/worker/critic role vocabulary. */
@Serializable
data class BackendPalette(
    val items: List<BackendInfo> = emptyList(),
    val roles: List<String> = emptyList(),
)

/** A working profile draft the multi-turn refine flow + the form both edit. */
@Serializable
data class TeamDraft(
    val name: String = "",
    val roles: Map<String, List<TeamSlot>> = emptyMap(),
    val shape: String? = null,
    @SerialName("protocol_bounds") val protocolBounds: ProtocolBounds? = null,
)

/** One prior turn of the builder chat, threaded back to the assistant each round. */
@Serializable
data class ChatTurn(val role: String, val content: String)

/** One refine turn's result: the assistant's prose reply + the updated draft. */
@Serializable
data class RefineResult(
    val reply: String = "",
    val draft: TeamDraft = TeamDraft(),
    val raw: String = "",
    val error: String? = null,
)
