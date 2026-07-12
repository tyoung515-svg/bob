package com.bobclaw.ui.screens

import com.bobclaw.model.Action
import com.bobclaw.model.Capabilities
import com.bobclaw.model.ProtocolBounds
import com.bobclaw.model.Team
import com.bobclaw.model.TeamSlot
import com.bobclaw.ui.components.ActionDisposition
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * MS9-W6 — pure Ask-Bob-on-Teams composition logic (Team↔Draft mapping · the manager(apex) surface ·
 * the apply guardrail). Proves the finding is closed at the logic layer: an existing team is editable
 * (toDraft), the manager is a first-class extractable spot, and applying a team never silently writes
 * (it rides the same reversible D11/D12 disposition as the Ask-Bob bubble). The visual is a screenshot.
 */
class TeamComposeLogicTest {

    private fun slot(backend: String, rolePrompt: String = "") =
        TeamSlot(backend = backend, rolePrompt = rolePrompt)

    private fun createTeamAction(risk: String = "reversible") = Action(
        id = APPLY_TEAM_ACTION_ID,
        title = "Create a team",
        descriptionPlain = "Create a new custom team from a set of roles.",
        risk = risk,
        undoHint = "Delete the team to undo.",
        pageScope = listOf("teams"),
    )

    private fun caps(vararg actions: Action) = Capabilities(actions = actions.toList())

    // ── Manager (apex) is the manager ─────────────────────────────────────────
    @Test
    fun managerRole_is_apex() {
        assertEquals("apex", MANAGER_ROLE)
    }

    // ── Team → editable Draft (edit an EXISTING team, not only build new) ──────
    @Test
    fun toDraft_carries_builder_owned_fields_and_drops_builtin() {
        val team = Team(
            name = "cloud-heavy",
            builtin = true,
            roles = mapOf(
                "apex" to listOf(slot("claude_api")),
                "worker" to listOf(slot("deepseek_v4_flash")),
            ),
            shape = "fusion",
            protocolBounds = ProtocolBounds(maxUsd = 2.0, grounding = "off"),
        )
        val draft = team.toDraft()
        assertEquals("cloud-heavy", draft.name)
        assertEquals("fusion", draft.shape)
        assertEquals(2.0, draft.protocolBounds?.maxUsd)
        assertEquals("off", draft.protocolBounds?.grounding)
        assertEquals("claude_api", draft.roles["apex"]!![0].backend)
        assertEquals("deepseek_v4_flash", draft.roles["worker"]!![0].backend)
        // Round-trips into the manager surface unchanged.
        assertEquals("claude_api", draft.managerBackend())
    }

    // ── The manager surface: extract who holds it ─────────────────────────────
    @Test
    fun managerBackend_reads_the_apex_primary_slot() {
        val roles = mapOf(
            "apex" to listOf(slot("kimi_cli"), slot("claude_api")),
            "worker" to listOf(slot("local")),
        )
        assertEquals("kimi_cli", managerBackend(roles))
        assertEquals("kimi_cli", managerSlot(roles)!!.backend)
    }

    @Test
    fun managerBackend_null_when_unmanaged_or_unassigned() {
        assertNull(managerBackend(mapOf("worker" to listOf(slot("local")))))
        // An apex spot with a blank backend is "unassigned" → no holder yet (but the spot exists).
        val unassigned = mapOf("apex" to listOf(slot("")))
        assertNull(managerBackend(unassigned))
        assertTrue(managerSlot(unassigned) != null)
    }

    // ── cleanedRoles: preview exactly what core persists ──────────────────────
    @Test
    fun cleanedRoles_drops_blank_slots_and_empty_roles() {
        val draft = com.bobclaw.model.TeamDraft(
            name = "x",
            roles = mapOf(
                "apex" to listOf(slot("claude_api"), slot("")),
                "worker" to listOf(slot("")),  // becomes empty → dropped entirely
                "critic" to listOf(slot("local")),
            ),
        )
        val cleaned = cleanedRoles(draft)
        assertEquals(setOf("apex", "critic"), cleaned.keys)
        assertEquals(1, cleaned["apex"]!!.size)
        assertEquals("claude_api", cleaned["apex"]!![0].backend)
    }

    // ── Apply guardrail: reversible confirm-once, never a silent write ────────
    @Test
    fun applyTeamAction_finds_create_team_in_registry() {
        assertEquals(APPLY_TEAM_ACTION_ID, applyTeamAction(caps(createTeamAction()))!!.id)
        assertNull(applyTeamAction(caps()))
        assertNull(applyTeamAction(null))
    }

    @Test
    fun apply_reversible_confirms_once_then_executes() {
        val c = caps(createTeamAction("reversible"))
        assertEquals(ActionDisposition.CONFIRM_FIRST, applyTeamDisposition(c, emptySet()))
        assertEquals(ActionDisposition.EXECUTE, applyTeamDisposition(c, setOf(APPLY_TEAM_ACTION_ID)))
    }

    @Test
    fun apply_fails_safe_to_confirm_when_registry_absent() {
        // No registry (older gateway / degraded doc) still NEVER auto-writes silently.
        assertEquals(ActionDisposition.CONFIRM_FIRST, applyTeamDisposition(null, emptySet()))
        assertEquals(ActionDisposition.EXECUTE, applyTeamDisposition(null, setOf(APPLY_TEAM_ACTION_ID)))
    }

    @Test
    fun apply_gated_action_routes_to_approvals_never_executes() {
        // If the server ever marks create_team `gated`, the write is routed to Approvals, never fired.
        val c = caps(createTeamAction("gated"))
        assertEquals(ActionDisposition.ROUTE_TO_APPROVALS, applyTeamDisposition(c, emptySet()))
        assertEquals(
            ActionDisposition.ROUTE_TO_APPROVALS,
            applyTeamDisposition(c, setOf(APPLY_TEAM_ACTION_ID)),
        )
    }
}
