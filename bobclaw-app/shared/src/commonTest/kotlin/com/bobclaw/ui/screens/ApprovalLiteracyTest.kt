package com.bobclaw.ui.screens

import com.bobclaw.model.ApprovalItem
import com.bobclaw.model.ApprovalKind
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ApprovalLiteracyTest {

    private fun approval(
        actionType: String,
        details: JsonObject? = null,
        id: String = "a1",
        status: String = "pending",
    ) = ApprovalItem(
        id = id,
        userId = "u",
        actionType = actionType,
        status = status,
        createdAt = "2026-01-01T00:00:00Z",
        details = details,
    )

    // ── decide contract is pinned (audit: wrong-decide-payload guard) ─────────────
    @Test
    fun decision_verbs_match_the_gateway_vocabulary() {
        assertEquals("approve", APPROVAL_DECISION_APPROVE)
        assertEquals("reject", APPROVAL_DECISION_REJECT)
    }

    // ── Simple/Pro fetch + diff policy (SPEC §6/§7) ───────────────────────────────
    @Test
    fun simple_auto_fetches_pro_does_not() {
        assertTrue(shouldAutoFetchLiteracy(EXPERIENCE_SIMPLE))
        assertFalse(shouldAutoFetchLiteracy(EXPERIENCE_PRO))
        // default-simple posture: an unexpected value is treated as simple (auto), never pro
        assertTrue(shouldAutoFetchLiteracy("anything-else"))
    }

    @Test
    fun raw_diff_shows_for_pro_only() {
        assertTrue(showRawDiff(EXPERIENCE_PRO))
        assertFalse(showRawDiff(EXPERIENCE_SIMPLE))
        assertFalse(showRawDiff("garbage"))
    }

    // ── LiteracyCache keyed per approval id ───────────────────────────────────────
    @Test
    fun cache_put_get_has_and_miss() {
        val cache = LiteracyCache()
        assertFalse(cache.has("a1"))
        assertNull(cache.get("a1"))
        cache.put("a1", "hello")
        assertTrue(cache.has("a1"))
        assertEquals("hello", cache.get("a1"))
        assertEquals(1, cache.size)
        // distinct ids do not collide
        cache.put("a2", "world")
        assertEquals(2, cache.size)
        assertEquals("world", cache.get("a2"))
        // overwrite same id
        cache.put("a1", "again")
        assertEquals("again", cache.get("a1"))
        assertEquals(2, cache.size)
    }

    // ── literacyPrompt (plain-language + pros/cons, calibrated by level) ───────────
    @Test
    fun prompt_names_the_action_and_asks_for_pros_and_cons() {
        val p = literacyPrompt(approval("task_approval"), EXPERIENCE_SIMPLE)
        assertTrue(p.contains("task_approval"), "prompt must name the action type")
        assertTrue(p.contains("PROS"), "prompt must ask for pros")
        assertTrue(p.contains("CONS"), "prompt must ask for cons")
        assertTrue(p.contains("Do NOT tell me what to choose"), "must forbid the face from deciding")
    }

    @Test
    fun prompt_calibrates_wording_by_experience_level() {
        val simple = literacyPrompt(approval("cc_edit"), EXPERIENCE_SIMPLE)
        val pro = literacyPrompt(approval("cc_edit"), EXPERIENCE_PRO)
        assertTrue(simple.contains("plain, everyday language"), "simple must ask for plain language")
        assertTrue(pro.contains("expert"), "pro must ask for an expert-level answer")
        assertTrue(simple != pro, "the two levels must produce different prompts")
    }

    @Test
    fun prompt_includes_details_when_present() {
        val a = approval("task_approval", buildJsonObject { put("command", "rm -rf /tmp/x") })
        assertTrue(literacyPrompt(a, EXPERIENCE_SIMPLE).contains("rm -rf /tmp/x"))
    }

    // ── ccEditDiff extraction ─────────────────────────────────────────────────────
    @Test
    fun cc_edit_diff_is_extracted_from_common_keys() {
        val d = approval("cc_edit", buildJsonObject { put("diff", "--- a\n+++ b\n+added") })
        assertEquals("--- a\n+++ b\n+added", ccEditDiff(d))

        val p = approval("cc_edit", buildJsonObject { put("patch", "@@ -1 +1 @@") })
        assertEquals("@@ -1 +1 @@", ccEditDiff(p))
    }

    @Test
    fun cc_edit_diff_null_for_non_cc_edit_or_missing_diff() {
        // right kind but no diff field
        assertNull(ccEditDiff(approval("cc_edit", buildJsonObject { put("note", "nope") })))
        // diff-bearing details but wrong kind
        assertNull(ccEditDiff(approval("task_approval", buildJsonObject { put("diff", "x") })))
        // no details at all
        assertNull(ccEditDiff(approval("cc_edit", null)))
        // non-string diff value is ignored (defensive)
        assertNull(ccEditDiff(approval("cc_edit", buildJsonObject { put("diff", buildJsonArray { add(1) }) })))
    }

    // ── approvalSummary ───────────────────────────────────────────────────────────
    @Test
    fun summary_prefers_summary_then_falls_through_to_other_keys() {
        assertEquals(
            "Send the weekly digest",
            approvalSummary(approval("task_approval", buildJsonObject { put("summary", "Send the weekly digest") })),
        )
        // no summary → next candidate (command)
        assertEquals(
            "ls -la",
            approvalSummary(approval("task_approval", buildJsonObject { put("command", "ls -la") })),
        )
    }

    @Test
    fun summary_empty_when_no_details_and_raw_fallback_otherwise() {
        assertEquals("", approvalSummary(approval("task_approval", null)))
        // unknown keys → compact raw JSON (non-empty), never a crash
        val raw = approvalSummary(approval("task_approval", buildJsonObject { put("weird", "value") }))
        assertTrue(raw.contains("weird"))
    }

    // ── kinds map lookups (display-only enrichment) ───────────────────────────────
    private val kinds = listOf(
        ApprovalKind(actionType = "cc_edit", label = "Code edit", proposalOnly = true, description = "A proposed diff."),
        ApprovalKind(actionType = "task_approval", label = "Task approval", proposalOnly = false, description = "A gated task."),
    )

    @Test
    fun kind_label_resolves_known_and_falls_back_to_raw() {
        assertEquals("Code edit", kindLabelFor(kinds, "cc_edit"))
        assertEquals("worker_scope_review", kindLabelFor(kinds, "worker_scope_review")) // unknown → raw
        assertEquals("Approval", kindLabelFor(emptyList(), "")) // blank + empty map → generic
    }

    @Test
    fun kind_description_and_proposal_only_flags() {
        assertEquals("A proposed diff.", kindDescriptionFor(kinds, "cc_edit"))
        assertEquals("", kindDescriptionFor(kinds, "unknown"))
        assertTrue(isProposalOnly(kinds, "cc_edit"))
        assertFalse(isProposalOnly(kinds, "task_approval"))
        assertFalse(isProposalOnly(kinds, "unknown"))
    }
}
