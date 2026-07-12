package com.bobclaw.ui.screens

import com.bobclaw.model.ApprovalItem
import com.bobclaw.model.ApprovalKind
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * Pure, Compose-free logic for the U6 Approvals screen (SPEC §7 · §6). Kept out of the composable so
 * it is fully unit-testable (mirrors `AskBobLogic` / `SlashPaletteLogic`): the screen renders these
 * decisions, it does not re-derive them.
 *
 * Fence: DISPLAY only. Nothing here alters approval semantics, gates, or the decide contract — the
 * decision verbs are pinned to the gateway's `POST /approvals/{id}/decide {decision}` vocabulary.
 */

// ── Decide contract (pinned to the gateway `_VALID_DECISIONS`) ──────────────────────────────────
/** The ONLY two decision verbs the gateway accepts. Pinned here (and asserted) so a display-layer
 *  refactor can never send a wrong payload — the audit's "wrong decide payload" guard. */
const val APPROVAL_DECISION_APPROVE = "approve"
const val APPROVAL_DECISION_REJECT = "reject"

// ── Simple/Pro (SPEC §6; the same experience_level knob U9 owns app-wide) ───────────────────────
const val EXPERIENCE_SIMPLE = "simple"
const val EXPERIENCE_PRO = "pro"

/**
 * The literacy fetch policy (SPEC §7): **Simple auto-fetches** the plain-language explanation,
 * **Pro fetches on click**. Default-simple posture: anything that is not explicitly `pro` auto-fetches
 * (the pref defaults to `simple` and is coerced valid upstream, so in practice this is exactly
 * simple⇒true / pro⇒false).
 */
fun shouldAutoFetchLiteracy(experienceLevel: String): Boolean = experienceLevel != EXPERIENCE_PRO

/** True when the raw diff should be rendered inline (Pro surface keeps the technical `cc_edit` diff). */
fun showRawDiff(experienceLevel: String): Boolean = experienceLevel == EXPERIENCE_PRO

/**
 * An in-memory explanation cache keyed by **approval id** (SPEC §7: "cached per approval id"). A fetched
 * explanation is reused for the life of the screen so re-expanding an item — or a Pro user clicking
 * "Explain" a second time — never re-hits the face. In-memory is fine (explanations are ephemeral).
 */
class LiteracyCache {
    private val store = mutableMapOf<String, String>()
    fun has(id: String): Boolean = store.containsKey(id)
    fun get(id: String): String? = store[id]
    fun put(id: String, text: String) { store[id] = text }
    val size: Int get() = store.size
}

/**
 * Build the plain-language prompt for the "cheap assistant-face call" (SPEC §7). Calibrated by
 * [experienceLevel] (SPEC §6): Simple ⇒ everyday language, no jargon; Pro ⇒ concise + technical.
 * Asks for a short explanation plus a PROS / CONS split, and explicitly forbids the face from
 * deciding for the user (display-only literacy, not an auto-approver).
 */
fun literacyPrompt(approval: ApprovalItem, experienceLevel: String): String {
    val audience = if (experienceLevel == EXPERIENCE_PRO) {
        "Write for an expert user: be concise and technically precise (jargon is fine)."
    } else {
        "Write in plain, everyday language for a non-technical person. Avoid jargon and acronyms."
    }
    return buildString {
        append("I have a pending approval that needs my decision. ")
        append("Action type: ").append(approval.actionType).append(". ")
        val details = approval.details?.toString()
        if (!details.isNullOrBlank() && details != "{}") {
            append("Details: ").append(details).append(". ")
        }
        append(audience).append(' ')
        append("In 3-5 short sentences, explain what approving this would actually do. ")
        append("Then give two short bullet lists: PROS (why I might approve) and CONS (why I might reject). ")
        append("Do NOT tell me what to choose — just help me understand it.")
    }
}

/** Candidate detail keys that carry a `cc_edit` unified diff / patch, in preference order. */
private val DIFF_KEYS = listOf("diff", "unified_diff", "patch", "content", "new_content")

/**
 * Extract the `cc_edit` diff text for the Pro diff view, or null. Returns the first non-blank string
 * among [DIFF_KEYS] in `details`, and ONLY for a `cc_edit` action (other kinds carry no diff). Pure —
 * unit-tested against `JsonObject` fixtures; the composable renders whatever string this returns.
 */
fun ccEditDiff(approval: ApprovalItem): String? {
    if (approval.actionType != "cc_edit") return null
    val details = approval.details ?: return null
    return firstStringField(details, DIFF_KEYS)
}

/** Candidate detail keys that make a decent one-line human summary of an approval, in order. */
private val SUMMARY_KEYS = listOf(
    "summary", "description", "title", "reason",
    "path", "file", "file_path", "command", "cmd", "url", "recipient", "subject",
)

/**
 * A short, human one-liner describing the approval, drawn from the first present [SUMMARY_KEYS]
 * string in `details`. Falls back to a compact form of the raw details, then to empty. Never throws.
 */
fun approvalSummary(approval: ApprovalItem): String {
    val details = approval.details ?: return ""
    firstStringField(details, SUMMARY_KEYS)?.let { return it }
    // Fallback: a compact rendering of the raw JSON object (already null-safe / bounded by caller UI).
    val raw = details.toString()
    return if (raw == "{}") "" else raw
}

/** First non-blank string primitive among [keys] in [obj], or null. Non-string / missing keys skip. */
private fun firstStringField(obj: JsonObject, keys: List<String>): String? {
    for (key in keys) {
        val el = obj[key] ?: continue
        val s = runCatching { el.jsonPrimitive.contentOrNull }.getOrNull()
        if (!s.isNullOrBlank()) return s
    }
    return null
}

/** Friendly label for a raw `action_type` via the server kinds map; falls back to the raw type. */
fun kindLabelFor(kinds: List<ApprovalKind>, actionType: String): String =
    kinds.firstOrNull { it.actionType == actionType }?.label?.takeIf { it.isNotBlank() }
        ?: actionType.ifBlank { "Approval" }

/** One-line description for a kind (from the server kinds map), or empty when unknown. */
fun kindDescriptionFor(kinds: List<ApprovalKind>, actionType: String): String =
    kinds.firstOrNull { it.actionType == actionType }?.description ?: ""

/** True when the kind is proposal-only (never auto-applies) — badge-worthy on the card. */
fun isProposalOnly(kinds: List<ApprovalKind>, actionType: String): Boolean =
    kinds.firstOrNull { it.actionType == actionType }?.proposalOnly == true

/**
 * The face-call seam for the literacy layer. A single suspend method the composable calls; the app
 * wiring supplies a chat-WS-backed implementation (see `ApprovalsScreen`). Pure interface so tests /
 * previews can stub it without a network.
 */
fun interface ApprovalExplainer {
    suspend fun explain(approval: ApprovalItem, experienceLevel: String): String
}
