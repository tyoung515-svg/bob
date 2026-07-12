package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One recognized approval kind + its display/policy metadata, served read-only by the gateway
 * `GET /approvals/kinds` (see `bobclaw-gateway/routers/approvals.py::KNOWN_APPROVAL_KINDS`). U6 uses
 * it purely for DISPLAY: a friendly [label] for a raw `action_type`, a one-line [description], and
 * the [proposalOnly] flag (a kind that NEVER auto-applies, e.g. `cc_edit` / `forest_fork`). The
 * approvals surface itself stays action_type-agnostic — an unknown kind still lists/decides through
 * the same endpoints; the kinds map only enriches the label. All fields defaulted so a partial /
 * evolving server payload never breaks the client.
 */
@Serializable
data class ApprovalKind(
    @SerialName("action_type") val actionType: String? = null,
    val label: String = "",
    @SerialName("proposal_only") val proposalOnly: Boolean = false,
    @SerialName("requires_human") val requiresHuman: Boolean = true,
    val description: String = "",
)
