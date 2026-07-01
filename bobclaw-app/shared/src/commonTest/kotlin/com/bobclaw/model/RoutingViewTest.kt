package com.bobclaw.model

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull

class RoutingViewTest {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    @Test
    fun decodesSnakeCaseRoutingViewIncludingLiveProbe() {
        val payload = """
            {
              "active_team": null,
              "teams": ["cloud-heavy", "demo-fleet", "local-first"],
              "live_probe": false,
              "faces": [
                {"id": "worker-deepseek", "role": "worker",
                 "preferred_backend": "deepseek_v4_flash",
                 "resolved_backend": "deepseek_v4_flash",
                 "escalation_chain": ["kimi_code"], "tool_capable": false},
                {"id": "builder-bob", "role": null,
                 "preferred_backend": "local", "resolved_backend": "local",
                 "escalation_chain": [], "tool_capable": false}
              ]
            }
        """.trimIndent()

        val view = json.decodeFromString<RoutingView>(payload)

        assertNull(view.activeTeam)
        assertFalse(view.liveProbe, "v0 routing-view must report live_probe=false")
        assertEquals(listOf("cloud-heavy", "demo-fleet", "local-first"), view.teams)
        assertEquals(2, view.faces.size)

        val deepseek = view.faces.first { it.id == "worker-deepseek" }
        assertEquals("worker", deepseek.role)
        assertEquals("deepseek_v4_flash", deepseek.resolvedBackend)
        assertEquals(listOf("kimi_code"), deepseek.escalationChain)

        assertNull(view.faces.first { it.id == "builder-bob" }.role, "unset role stays honestly null")
    }
}
