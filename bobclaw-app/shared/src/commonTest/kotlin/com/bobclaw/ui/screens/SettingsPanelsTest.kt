package com.bobclaw.ui.screens

import com.bobclaw.model.ApprovalKind
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Unit coverage for the pure U10 Settings-pane logic ([SettingsPanels]): base64url decode, JWT claim
 * extraction (identity + exp), token-expiry formatting, and the approval-defaults mapping. All
 * Compose-free — the panes themselves are covered by the green desktop compile + the screenshot pass.
 *
 * JWT vectors are real 3-segment tokens (header.payload.sig) whose payloads base64url-decode to the
 * JSON shown; the signature is intentionally junk (we never verify it — display-only).
 */
class SettingsPanelsTest {

    // payload = {"sub":"admin","iat":1720000000,"exp":1720003600}
    private val FULL =
        "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJzdWIiOiAiYWRtaW4iLCAiaWF0IjogMTcyMDAwMDAwMCwgImV4cCI6IDE3MjAwMDM2MDB9.sig_ignored"
    // payload = {"sub":"neckbeard"}   (no exp)
    private val NOEXP =
        "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJzdWIiOiAibmVja2JlYXJkIn0.sig_ignored"
    // payload = {"email":"t@example.com","exp":1720003600}   (no sub → falls back to email)
    private val EMAIL =
        "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJlbWFpbCI6ICJ0QGV4YW1wbGUuY29tIiwgImV4cCI6IDE3MjAwMDM2MDB9.sig_ignored"
    // payload = {"sub":"a"}   (single-char → payload segment has NO base64 padding)
    private val PADCHK =
        "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJzdWIiOiAiYSJ9.sig_ignored"

    // ---- base64url ----

    @Test
    fun base64url_decodes_a_json_payload() {
        val json = base64UrlDecodeToString("eyJzdWIiOiAiYSJ9")   // {"sub": "a"}
        assertEquals("{\"sub\": \"a\"}", json)
    }

    @Test
    fun base64url_rejects_garbage_and_empty() {
        // '+' and '/' are base64 (NOT base64url) → rejected; empty → null.
        assertNull(base64UrlDecodeToString("not+valid/base64url"))
        assertNull(base64UrlDecodeToString(""))
    }

    // ---- JWT identity ----

    @Test
    fun identity_prefers_sub() {
        assertEquals("admin", jwtIdentity(FULL))
        assertEquals("neckbeard", jwtIdentity(NOEXP))
    }

    @Test
    fun identity_falls_back_to_email_when_no_sub() {
        assertEquals("t@example.com", jwtIdentity(EMAIL))
    }

    @Test
    fun identity_is_null_for_absent_blank_and_malformed_tokens() {
        assertNull(jwtIdentity(null))
        assertNull(jwtIdentity(""))
        assertNull(jwtIdentity("   "))
        assertNull(jwtIdentity("garbage-not-a-jwt"))
        assertNull(jwtIdentity("only.two"))      // 3 parts but payload isn't valid JSON base64url
    }

    @Test
    fun single_char_subject_decodes_without_base64_padding() {
        // Proves the padding-optional path: this payload segment is not a multiple of 4 chars.
        assertEquals("a", jwtIdentity(PADCHK))
    }

    // ---- JWT exp ----

    @Test
    fun exp_is_read_as_epoch_seconds() {
        assertEquals(1720003600L, jwtExpEpochSeconds(FULL))
    }

    @Test
    fun exp_is_null_when_absent_or_token_bad() {
        assertNull(jwtExpEpochSeconds(NOEXP))
        assertNull(jwtExpEpochSeconds(null))
        assertNull(jwtExpEpochSeconds("garbage"))
    }

    // ---- expiry math + formatting ----

    @Test
    fun expiry_minutes_is_positive_before_and_negative_after() {
        // exp = 1720003600. 30 min before → +30; 5 min after → -5; null exp → null.
        assertEquals(30L, tokenExpiryMinutes(1720003600L, 1720003600L - 1800))
        assertEquals(-5L, tokenExpiryMinutes(1720003600L, 1720003600L + 300))
        assertNull(tokenExpiryMinutes(null, 1720003600L))
    }

    @Test
    fun duration_formats_short_and_locale_neutral() {
        assertEquals("0m", formatDurationShort(0))
        assertEquals("0m", formatDurationShort(-10))
        assertEquals("42m", formatDurationShort(42))
        assertEquals("1h", formatDurationShort(60))
        assertEquals("2h 05m", formatDurationShort(125))
        assertEquals("1h 30m", formatDurationShort(90))
    }

    // ---- approval-defaults mapping (read-only view of current defaults v1) ----

    @Test
    fun approval_defaults_map_sort_and_fall_back_labels() {
        val kinds = listOf(
            ApprovalKind(actionType = "cc_edit", label = "Code edit", proposalOnly = true, requiresHuman = true, description = "d1"),
            ApprovalKind(actionType = "web_fetch", label = "", proposalOnly = false, requiresHuman = true, description = "d2"),
            ApprovalKind(actionType = null, label = "", proposalOnly = false, requiresHuman = false, description = "d3"),
        )
        val rows = approvalDefaultRows(kinds)
        assertEquals(3, rows.size)
        // sorted by label (case-insensitive): "Code edit" < "unknown" < "web_fetch"
        assertEquals(listOf("Code edit", "unknown", "web_fetch"), rows.map { it.label })
        // blank label falls back to action_type; null action_type falls back to "unknown"
        assertEquals("web_fetch", rows.first { it.key == "web_fetch" }.label)
        assertEquals("unknown", rows.first { it.key == "" }.label)
        // flags carried through verbatim
        val ccEdit = rows.first { it.key == "cc_edit" }
        assertTrue(ccEdit.proposalOnly)
        assertTrue(ccEdit.requiresHuman)
    }

    @Test
    fun approval_defaults_empty_maps_to_empty() {
        assertTrue(approvalDefaultRows(emptyList()).isEmpty())
    }
}
