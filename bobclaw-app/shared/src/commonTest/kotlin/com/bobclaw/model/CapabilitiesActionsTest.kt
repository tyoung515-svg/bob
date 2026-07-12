package com.bobclaw.model

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * U5 — the app deserializes the U3 `actions` section of `GET /capabilities` (the section the
 * helper bubble filters by page_scope). Tolerant: unknown keys ignored, opaque params_schema/binding.
 */
class CapabilitiesActionsTest {

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    // A trimmed but faithful slice of the gateway /capabilities document (U2 faces + U3 actions).
    private val doc = """
        {
          "faces": [],
          "backends": [],
          "actions": [
            {
              "id": "create_team",
              "title": "Create a team",
              "description_plain": "Create a new custom team from a set of roles.",
              "params_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
              "risk": "reversible",
              "undo_hint": "Delete the team to undo.",
              "page_scope": ["teams"],
              "binding": {"kind": "rest", "method": "POST", "path": "/teams", "fixed_params": {}}
            },
            {
              "id": "forget_fact",
              "title": "Forget a memory fact",
              "description_plain": "Permanently delete a stored memory fact.",
              "params_schema": {"type": "object"},
              "risk": "gated",
              "undo_hint": null,
              "page_scope": ["memory"],
              "binding": {"kind": "rest", "method": "DELETE", "path": "/memory/facts/{fact_id}"}
            },
            {
              "id": "pin_face",
              "title": "Pin a face",
              "description_plain": "Pin a specific Bob face to this conversation.",
              "risk": "reversible",
              "undo_hint": "Clear the pin.",
              "page_scope": ["chat"],
              "binding": {"kind": "ws", "ws_type": "switch_face"}
            }
          ],
          "capabilities": {"roles": [], "face_count": 0, "backend_count": 0, "available_backends": [], "action_count": 3},
          "unexpected_future_field": 42
        }
    """.trimIndent()

    @Test
    fun deserializes_actions_with_risk_scope_and_binding() {
        val caps = json.decodeFromString(Capabilities.serializer(), doc)
        assertEquals(3, caps.actions.size)
        assertEquals(3, caps.capabilities.actionCount)

        val createTeam = caps.actions.first { it.id == "create_team" }
        assertEquals("reversible", createTeam.risk)
        assertEquals(listOf("teams"), createTeam.pageScope)
        assertEquals("Delete the team to undo.", createTeam.undoHint)
        assertEquals("rest", createTeam.binding?.kind)
        assertEquals("POST", createTeam.binding?.method)

        val forget = caps.actions.first { it.id == "forget_fact" }
        assertEquals("gated", forget.risk)
        assertNull(forget.undoHint)

        val pin = caps.actions.first { it.id == "pin_face" }
        assertEquals("ws", pin.binding?.kind)
        assertEquals("switch_face", pin.binding?.wsType)
    }

    @Test
    fun older_gateway_without_actions_still_deserializes_empty() {
        val caps = json.decodeFromString(Capabilities.serializer(), """{"faces": [], "backends": []}""")
        assertTrue(caps.actions.isEmpty())
    }
}
