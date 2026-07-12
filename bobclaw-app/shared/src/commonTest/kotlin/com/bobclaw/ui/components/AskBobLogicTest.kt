package com.bobclaw.ui.components

import com.bobclaw.model.Action
import com.bobclaw.model.Capabilities
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * U5 Ask-Bob helper bubble — the pure D11-tier + D12-guardrail logic. These prove accept #3
 * (a reversible action executes with consequence toast + undo + confirm-once) and #4 (a gated
 * action lands in Approvals, never executes directly) at the logic layer; the visual is a screenshot.
 */
class AskBobLogicTest {

    private fun action(
        id: String,
        risk: String,
        pageScope: List<String> = listOf("teams"),
        undoHint: String? = if (risk == "reversible") "undo it" else null,
        title: String = id,
        desc: String = "does $id",
    ) = Action(
        id = id,
        title = title,
        descriptionPlain = desc,
        risk = risk,
        undoHint = undoHint,
        pageScope = pageScope,
    )

    // ── Tool scope: page_scope filtering ──────────────────────────────────────
    @Test
    fun actionsForPage_filters_by_page_scope_case_insensitive() {
        val caps = Capabilities(
            actions = listOf(
                action("create_team", "reversible", pageScope = listOf("teams")),
                action("forget_fact", "gated", pageScope = listOf("memory")),
                action("new_conversation", "reversible", pageScope = listOf("home", "chat")),
            )
        )
        assertEquals(listOf("create_team"), actionsForPage(caps, "teams").map { it.id })
        assertEquals(listOf("create_team"), actionsForPage(caps, "TEAMS").map { it.id })
        assertEquals(listOf("new_conversation"), actionsForPage(caps, "home").map { it.id })
        assertEquals(emptyList(), actionsForPage(caps, "approvals").map { it.id })
    }

    @Test
    fun actionsForPage_null_or_blank_is_empty() {
        assertTrue(actionsForPage(null, "teams").isEmpty())
        assertTrue(actionsForPage(Capabilities(actions = listOf(action("x", "read"))), "  ").isEmpty())
    }

    // ── D11 tiers + D12 confirm-once + rate cap ───────────────────────────────
    @Test
    fun gated_action_routes_to_approvals_never_executes() {
        // Accept #4: a gated action NEVER returns EXECUTE from the bubble, in any state.
        val gated = action("forget_fact", "gated")
        assertEquals(ActionDisposition.ROUTE_TO_APPROVALS, dispositionFor(gated, emptySet(), 0))
        assertEquals(
            ActionDisposition.ROUTE_TO_APPROVALS,
            dispositionFor(gated, setOf("forget_fact"), 0), // even if somehow "confirmed"
        )
    }

    @Test
    fun unknown_risk_tier_fails_safe_to_approvals() {
        assertEquals(
            ActionDisposition.ROUTE_TO_APPROVALS,
            dispositionFor(action("weird", "explode"), emptySet(), 0),
        )
    }

    @Test
    fun read_action_executes_without_confirm() {
        assertEquals(ActionDisposition.EXECUTE, dispositionFor(action("look", "read"), emptySet(), 0))
    }

    @Test
    fun reversible_action_confirms_once_then_executes() {
        // Accept #3: first use → CONFIRM_FIRST; once the id is confirmed → EXECUTE.
        val a = action("create_team", "reversible")
        assertEquals(ActionDisposition.CONFIRM_FIRST, dispositionFor(a, emptySet(), 0))
        assertEquals(ActionDisposition.EXECUTE, dispositionFor(a, setOf("create_team"), 0))
    }

    @Test
    fun mutating_rate_cap_refuses_beyond_budget() {
        val a = action("create_team", "reversible")
        val confirmed = setOf("create_team")
        assertEquals(ActionDisposition.EXECUTE, dispositionFor(a, confirmed, MUTATING_RATE_CAP - 1))
        assertEquals(ActionDisposition.RATE_CAPPED, dispositionFor(a, confirmed, MUTATING_RATE_CAP))
        // A read action is never rate-capped (not mutating).
        assertEquals(ActionDisposition.EXECUTE, dispositionFor(action("look", "read"), confirmed, MUTATING_RATE_CAP + 5))
    }

    @Test
    fun isMutating_true_for_write_tiers_false_for_read() {
        assertTrue(isMutating(action("create_team", "reversible")))
        assertTrue(isMutating(action("forget_fact", "gated")))
        assertFalse(isMutating(action("look", "read")))
    }

    // ── D12 consequence toast ─────────────────────────────────────────────────
    @Test
    fun consequence_toast_names_action_and_undo() {
        val a = action("delete_team", "reversible", undoHint = "restore from cache", title = "Delete a team", desc = "Deletes a custom team.")
        val toast = consequenceToast(a)
        assertTrue(toast.contains("Delete a team"))
        assertTrue(toast.contains("Deletes a custom team."))
        assertTrue(toast.contains("Undo: restore from cache"))
    }

    @Test
    fun consequence_toast_without_undo_omits_undo_clause() {
        val a = action("look", "read", undoHint = null, title = "Look", desc = "Reads state.")
        val toast = consequenceToast(a)
        assertTrue(toast.contains("Look"))
        assertFalse(toast.contains("Undo:"))
    }
}
