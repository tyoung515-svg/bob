package com.bobclaw.ui.screens

import com.bobclaw.model.ApprovalKind
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.longOrNull

/**
 * Pure, Compose-free logic backing the U10 Settings panes (Account / Models / Connections /
 * Approval-defaults). Deliberately UI-free so the load-bearing bits — JWT claim decode, token-expiry
 * formatting, and the approval-defaults mapping — are provable in `commonTest` with no Compose runtime.
 *
 * Everything here is **display-only and read-only**: we NEVER verify a JWT signature (the gateway
 * owns that) — we only base64url-decode the token payload to *show* the identity + expiry the user is
 * already holding. No endpoint is invented; a missing datum degrades to null and the pane shows "—".
 */

private val LENIENT_JSON = Json { ignoreUnknownKeys = true; isLenient = true }

// ---- JWT (Account pane: identity + token expiry) ---------------------------------------------

/**
 * base64url (RFC 4648 §5, padding optional) → decoded UTF-8 string, or null on any malformed input.
 * Dependency-free so it can live in commonMain (no `java.util.Base64`): a streaming 6→8-bit regroup.
 */
internal fun base64UrlDecodeToString(input: String): String? {
    val s = input.trim().trimEnd('=')
    if (s.isEmpty()) return null
    val alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    val out = ArrayList<Byte>((s.length * 3) / 4 + 3)
    var buffer = 0
    var bits = 0
    for (c in s) {
        val idx = alphabet.indexOf(c)
        if (idx < 0) return null                       // any non-alphabet char ⇒ not base64url
        buffer = (buffer shl 6) or idx
        bits += 6
        if (bits >= 8) {
            bits -= 8
            out.add(((buffer shr bits) and 0xFF).toByte())
        }
    }
    return runCatching { out.toByteArray().decodeToString() }.getOrNull()
}

/**
 * Decode a JWT's payload (the middle `.`-segment) into a [JsonObject], or null when [token] is
 * null/blank or not a well-formed JWT with a JSON-object payload. Signature is NOT checked (the
 * gateway validates it on every call — this is purely to display what the client already holds).
 */
fun decodeJwtPayload(token: String?): JsonObject? {
    if (token.isNullOrBlank()) return null
    val parts = token.split(".")
    if (parts.size < 2) return null
    val payloadJson = base64UrlDecodeToString(parts[1]) ?: return null
    return runCatching { LENIENT_JSON.parseToJsonElement(payloadJson).jsonObject }.getOrNull()
}

/** The JWT `exp` claim as epoch SECONDS, or null when absent / not a number. */
fun jwtExpEpochSeconds(token: String?): Long? =
    (decodeJwtPayload(token)?.get("exp") as? JsonPrimitive)?.longOrNull

/**
 * A human identity from the token. BoBClaw's gateway sets `sub` = user_id (bobclaw-gateway/auth.py
 * `create_access_token`), so we prefer `sub`, then fall back to common OIDC-ish claims for
 * robustness. Null when the token carries no identity claim.
 */
fun jwtIdentity(token: String?): String? {
    val payload = decodeJwtPayload(token) ?: return null
    for (claim in listOf("sub", "preferred_username", "email", "username", "name")) {
        val v = (payload[claim] as? JsonPrimitive)?.contentOrNull
        if (!v.isNullOrBlank()) return v
    }
    return null
}

/** Whole minutes until [expEpochSeconds] from [nowEpochSeconds]; negative once expired, null if absent. */
fun tokenExpiryMinutes(expEpochSeconds: Long?, nowEpochSeconds: Long): Long? {
    if (expEpochSeconds == null) return null
    return (expEpochSeconds - nowEpochSeconds) / 60
}

/**
 * Format a positive minute count as a short, locale-neutral duration ("42m", "1h", "2h 05m") — same
 * philosophy as [formatScale] (a machine-ish value, not translated copy). Values <= 0 clamp to "0m";
 * the caller decides "Expired" vs "expires in <this>".
 */
fun formatDurationShort(minutes: Long): String {
    if (minutes <= 0) return "0m"
    val h = minutes / 60
    val m = minutes % 60
    return when {
        h == 0L -> "${m}m"
        m == 0L -> "${h}h"
        else -> "${h}h ${if (m < 10) "0$m" else "$m"}m"
    }
}

// ---- Approval-defaults (Approvals pane: read-only view of current v1 defaults) ----------------

/**
 * A single flattened, display-ready approval-default row. Sourced read-only from the gateway
 * `GET /approvals/kinds` metadata map ([ApprovalKind]) — this IS "current defaults v1": which action
 * kinds require a human and which can only ever be *proposed* (never auto-applied). Editing is
 * deferred; this is a view only.
 */
data class ApprovalDefaultRow(
    val key: String,
    val label: String,
    val proposalOnly: Boolean,
    val requiresHuman: Boolean,
    val description: String,
)

/**
 * Map the raw [ApprovalKind] metadata to sorted, display-ready [ApprovalDefaultRow]s. Pure: label
 * falls back to the action_type (then "unknown"); rows are ordered by label for a stable render.
 * Nothing here can mutate a default — it is strictly a read projection.
 */
fun approvalDefaultRows(kinds: List<ApprovalKind>): List<ApprovalDefaultRow> =
    kinds.map { k ->
        val key = k.actionType?.takeIf { it.isNotBlank() } ?: ""
        ApprovalDefaultRow(
            key = key,
            label = k.label.takeIf { it.isNotBlank() } ?: key.ifBlank { "unknown" },
            proposalOnly = k.proposalOnly,
            requiresHuman = k.requiresHuman,
            description = k.description,
        )
    }.sortedBy { it.label.lowercase() }
