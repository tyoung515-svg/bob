package com.bobclaw.ui.screens

import com.bobclaw.model.CapabilityBackend
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * MS9-W2 — headless guard for the Chat model/backend picker pure logic (build over the live GET
 * /capabilities backends). Runs under `./gradlew :shared:jvmTest`. Proves: friendly model names
 * replace raw backend ids, the Auto row clears the pin, availability is annotated, and the picker
 * degrades honestly to the static id list — all BEFORE the attended screenshot pass.
 */
class ModelPickerLogicTest {

    private val live = listOf(
        CapabilityBackend(backend = "claude_code", available = true, model = "Opus 4.8"),
        CapabilityBackend(backend = "minimax", available = false, model = "MiniMax-M2"),
        CapabilityBackend(backend = "local", available = true, model = null),
    )
    private val staticBackends = listOf("deepseek_v4_flash", "claude_code", "minimax")

    private fun build(
        live: List<CapabilityBackend> = this.live,
        selected: String? = null,
    ) = buildModelPickerOptions(
        liveBackends = live,
        staticBackends = staticBackends,
        selectedBackend = selected,
        autoLabel = "Auto",
        availableLabel = "available",
        unavailableLabel = "unavailable",
    )

    @Test
    fun auto_row_is_first_and_clears_the_pin() {
        val auto = build(selected = null).first()
        assertNull(auto.backendId)          // null backendId ⇒ applyBackend(null) ⇒ clears the pin
        assertEquals("Auto", auto.label)
        assertNull(auto.secondary)
        assertTrue(auto.selected)           // unpinned ⇒ Auto is the active row
    }

    @Test
    fun live_rows_use_friendly_model_names_not_backend_ids() {
        val opts = build()
        val opus = opts.first { it.backendId == "claude_code" }
        assertEquals("Opus 4.8", opus.label)            // friendly model name, NOT "claude_code"
        assertEquals("claude_code · available", opus.secondary)  // backend + availability annotation
        assertTrue(opus.available)
    }

    @Test
    fun unavailable_backend_is_annotated_and_dimmable() {
        val mm = build().first { it.backendId == "minimax" }
        assertEquals("MiniMax-M2", mm.label)
        assertEquals("minimax · unavailable", mm.secondary)
        assertFalse(mm.available)
    }

    @Test
    fun backend_without_a_model_falls_back_to_the_id() {
        val local = build().first { it.backendId == "local" }
        assertEquals("local", local.label)   // no registry model ⇒ show the id as the primary line
        assertEquals("available", local.secondary)  // still annotate availability
    }

    @Test
    fun selecting_a_backend_marks_only_that_row_selected() {
        val opts = build(selected = "claude_code")
        assertTrue(opts.first { it.backendId == "claude_code" }.selected)
        assertFalse(opts.first { it.backendId == null }.selected)     // Auto no longer selected
        assertEquals(1, opts.count { it.selected })
    }

    @Test
    fun empty_registry_degrades_to_the_static_ids_without_availability_claims() {
        val opts = build(live = emptyList(), selected = "claude_code")
        // Auto + the three static ids (no synthetic append — the pin is in the static list).
        assertEquals(1 + staticBackends.size, opts.size)
        val cc = opts.first { it.backendId == "claude_code" }
        assertEquals("claude_code", cc.label)   // bare id, no friendly name available yet
        assertNull(cc.secondary)                 // no availability claim when the registry is absent
        assertTrue(cc.selected)
        assertTrue(opts.drop(1).all { it.available })  // degraded rows are not dimmed
    }

    @Test
    fun off_registry_pin_stays_visible_as_a_synthetic_row() {
        val opts = build(selected = "renamed_backend")
        val ghost = opts.last()
        assertEquals("renamed_backend", ghost.backendId)
        assertEquals("renamed_backend", ghost.label)
        assertTrue(ghost.selected)
        assertEquals(1, opts.count { it.selected })   // exactly the pin, nothing else
        assertFalse(opts.first { it.backendId == null }.selected)
    }

    // ── MS9-W4 (fix D): the top-bar chip label derivation ────────────────────────────────────────

    @Test
    fun chip_label_shows_the_pinned_models_friendly_name_not_auto() {
        assertEquals("Opus 4.8", chatBackendChipLabel(build(selected = "claude_code"), "Auto"))
    }

    @Test
    fun chip_label_is_auto_only_when_truly_unpinned() {
        assertEquals("Auto", chatBackendChipLabel(build(selected = null), "Auto"))
    }

    @Test
    fun chip_label_shows_an_off_registry_pin_id_never_auto() {
        assertEquals("renamed_backend", chatBackendChipLabel(build(selected = "renamed_backend"), "Auto"))
    }

    @Test
    fun chip_label_degrades_to_the_bare_id_when_the_registry_is_absent() {
        assertEquals("claude_code", chatBackendChipLabel(build(live = emptyList(), selected = "claude_code"), "Auto"))
    }

    // ── R0: GPT-5.6 roster — one backend (codex_code), three distinct models ─────────────────────

    @Test
    fun codex_code_surfaces_three_distinct_gpt56_models_no_collapse() {
        val rows = buildModelPickerOptions(
            liveBackends = listOf(
                CapabilityBackend(backend = "codex_code", available = true, model = "GPT-5.6 Sol"),
                CapabilityBackend(backend = "codex_code", available = true, model = "GPT-5.6 Terra"),
                CapabilityBackend(backend = "codex_code", available = true, model = "GPT-5.6 Luna"),
            ),
            staticBackends = staticBackends,
            selectedBackend = null,
            autoLabel = "Auto",
            availableLabel = "Available",
            unavailableLabel = "Unavailable",
        )

        val modelRows = rows.drop(1)

        assertEquals(
            listOf("GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.6 Luna"),
            modelRows.map { it.label },
        )
        assertEquals(3, modelRows.map { it.label }.toSet().size)
        assertTrue(modelRows.all { it.backendId == "codex_code" })
    }
}
