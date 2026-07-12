package com.bobclaw.ui.screens

import com.bobclaw.model.Capabilities
import com.bobclaw.model.CapabilityBackend
import com.bobclaw.model.CapabilitySummary
import com.bobclaw.model.Face
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Headless guard for the chat `/` slash-palette pure logic (build + filter over the live GET
 * /capabilities document). Runs under `./gradlew :shared:jvmTest` — proves the palette lists the
 * registry in the right groups/order and filters correctly BEFORE the attended screenshot pass.
 */
class SlashPaletteLogicTest {

    private fun face(id: String, name: String, backend: String) =
        Face(id = id, name = name, avatar = "$id.png", preferredBackend = backend, uiTheme = "dark")

    private fun caps(): Capabilities = Capabilities(
        faces = listOf(
            face("planner-claude", "Planner", "claude_code"),
            face("scout-deepseek", "Scout", "deepseek_v4_flash"),
        ),
        backends = listOf(
            CapabilityBackend(backend = "deepseek_v4_flash", available = true, model = "deepseek-v4"),
            CapabilityBackend(backend = "kimi_code", available = false, model = null),
        ),
        capabilities = CapabilitySummary(
            roles = listOf("apex", "worker"),
            faceCount = 2,
            backendCount = 2,
            availableBackends = listOf("deepseek_v4_flash"),
        ),
    )

    @Test
    fun null_caps_yields_only_the_init_action() {
        val entries = buildPaletteEntries(null)
        assertEquals(1, entries.size)
        val only = entries.single()
        assertEquals(PaletteKind.ACTION, only.kind)
        assertEquals("init", only.id)
    }

    @Test
    fun build_orders_action_then_faces_then_backends() {
        val kinds = buildPaletteEntries(caps()).map { it.kind }
        assertEquals(
            listOf(
                PaletteKind.ACTION,
                PaletteKind.FACE, PaletteKind.FACE,
                PaletteKind.BACKEND, PaletteKind.BACKEND,
            ),
            kinds,
        )
    }

    @Test
    fun face_and_backend_entries_carry_registry_detail() {
        val entries = buildPaletteEntries(caps())
        val planner = entries.first { it.kind == PaletteKind.FACE && it.id == "planner-claude" }
        assertEquals("Planner", planner.label)
        assertEquals("claude_code", planner.detail)

        val ds = entries.first { it.kind == PaletteKind.BACKEND && it.id == "deepseek_v4_flash" }
        assertEquals("deepseek-v4", ds.detail)
        assertTrue(ds.available)

        // No model + unavailable → the availability word, dimmed.
        val kimi = entries.first { it.id == "kimi_code" }
        assertEquals("unavailable", kimi.detail)
        assertFalse(kimi.available)
    }

    @Test
    fun empty_query_returns_all_entries() {
        val all = buildPaletteEntries(caps())
        assertEquals(all, filterPaletteEntries(all, "   "))
    }

    @Test
    fun filter_matches_label_id_detail_and_kind_case_insensitively() {
        val all = buildPaletteEntries(caps())

        // by face name (label)
        assertTrue(filterPaletteEntries(all, "plan").any { it.id == "planner-claude" })
        // by backend name (id), even when unavailable
        assertTrue(filterPaletteEntries(all, "KIMI").any { it.id == "kimi_code" })
        // by detail (a backend's model string)
        assertTrue(filterPaletteEntries(all, "deepseek-v4").any { it.id == "deepseek_v4_flash" })
        // by kind name → every backend row
        val backends = filterPaletteEntries(all, "backend")
        assertEquals(2, backends.size)
        assertTrue(backends.all { it.kind == PaletteKind.BACKEND })
        // a miss returns nothing
        assertTrue(filterPaletteEntries(all, "zzz-no-such").isEmpty())
    }

    // ── R0: GPT-5.6 roster — Sol/Terra/Luna are distinct palette faces ───────────────────────────

    @Test
    fun gpt56_roster_faces_are_distinct_palette_entries() {
        val all = buildPaletteEntries(
            Capabilities(
                faces = listOf(
                    face("planner-gpt56-sol", "Planner (GPT-5.6 Sol)", "codex_code"),
                    face("worker-gpt56-terra", "Worker (GPT-5.6 Terra)", "codex_code"),
                    face("worker-gpt56-luna", "Worker (GPT-5.6 Luna)", "codex_code"),
                ),
                backends = emptyList(),
                capabilities = CapabilitySummary(
                    roles = listOf("apex", "worker"),
                    faceCount = 3,
                    backendCount = 0,
                    availableBackends = emptyList(),
                ),
            ),
        )
        val faceEntries = all.filter { it.kind == PaletteKind.FACE }

        assertEquals(3, faceEntries.size)
        assertEquals(
            listOf("planner-gpt56-sol", "worker-gpt56-terra", "worker-gpt56-luna"),
            faceEntries.map { it.id },
        )
        assertEquals(3, faceEntries.map { it.label }.toSet().size)

        val terraMatches = filterPaletteEntries(all, "terra")
        assertTrue(terraMatches.any { it.id == "worker-gpt56-terra" })
        assertFalse(terraMatches.any { it.id == "worker-gpt56-luna" })
    }
}
