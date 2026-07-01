package com.bobclaw.model

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TeamTest {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true; encodeDefaults = true }

    @Test
    fun decodesTeamWithMultiSlotRoles() {
        val payload = """
            {"name":"demo-fleet","builtin":true,
             "roles":{"worker":[{"name":"bulk","backend":"deepseek_v4_flash","escalation_chain":["glm_5_2","kimi_code"]},
                                {"name":"tool","backend":"glm_5_2","escalation_chain":[]}],
                      "critic":[{"name":"","backend":"local","escalation_chain":[]}]}}
        """.trimIndent()
        val team = json.decodeFromString<Team>(payload)
        assertEquals("demo-fleet", team.name)
        assertTrue(team.builtin)
        assertEquals(2, team.roles["worker"]!!.size)
        assertEquals("deepseek_v4_flash", team.roles["worker"]!![0].backend)
        assertEquals(listOf("glm_5_2", "kimi_code"), team.roles["worker"]!![0].escalationChain)
        assertEquals("tool", team.roles["worker"]!![1].name)
        assertTrue(team.roles["critic"]!![0].escalationChain.isEmpty())
    }

    @Test
    fun encodesSlotWithSnakeCaseEscalationChain() {
        val slot = TeamSlot(name = "lead", backend = "claude_api", escalationChain = listOf("claude_code"))
        val out = json.encodeToString(TeamSlot.serializer(), slot)
        assertTrue(out.contains("\"backend\":\"claude_api\""), out)
        assertTrue(out.contains("\"escalation_chain\":[\"claude_code\"]"), out)
    }

    @Test
    fun decodesBackendPalette() {
        val payload = """
            {"items":[{"backend":"local","max_usd_per_worker":0.0,"max_fanout_width":1},
                      {"backend":"deepseek_v4_flash","max_usd_per_worker":0.005,"max_fanout_width":20}],
             "roles":["apex","worker","critic"]}
        """.trimIndent()
        val pal = json.decodeFromString<BackendPalette>(payload)
        assertEquals(2, pal.items.size)
        assertEquals("local", pal.items[0].backend)
        assertEquals(20, pal.items[1].maxFanoutWidth)
        assertEquals(listOf("apex", "worker", "critic"), pal.roles)
        assertFalse(pal.items.isEmpty())
    }

    @Test
    fun decodesRefineResultWithDraftIgnoringExtraKeys() {
        val payload = """
            {"reply":"Made the worker cheaper.",
             "draft":{"name":"cheap","roles":{"worker":[{"name":"","backend":"deepseek_v4_flash","escalation_chain":[]}]}},
             "raw":"{...}"}
        """.trimIndent()
        val r = json.decodeFromString<RefineResult>(payload)
        assertEquals("Made the worker cheaper.", r.reply)
        assertEquals("cheap", r.draft.name)
        assertEquals("deepseek_v4_flash", r.draft.roles["worker"]!![0].backend)
        assertNull(r.error)
    }

    @Test
    fun decodesProfileWithRolePromptShapeAndBounds() {
        val payload = """
            {"name":"council-fast","builtin":false,
             "roles":{"worker":[{"backend":"deepseek_v4_flash","role_prompt":"be terse","escalation_chain":[]}]},
             "shape":"fusion","protocol_bounds":{"max_usd":2.0,"grounding":"off"}}
        """.trimIndent()
        val t = json.decodeFromString<Team>(payload)
        assertEquals("fusion", t.shape)
        assertEquals("be terse", t.roles["worker"]!![0].rolePrompt)
        assertEquals(2.0, t.protocolBounds!!.maxUsd)
        assertEquals("off", t.protocolBounds!!.grounding)
    }
}
