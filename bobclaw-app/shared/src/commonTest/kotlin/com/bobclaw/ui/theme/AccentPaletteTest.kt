package com.bobclaw.ui.theme

import androidx.compose.ui.graphics.Color
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Pure-logic tests for the lane-4b accent palette ([ACCENT_PRESETS] / [accentColorFor]).
 * No Compose-UI-test dependency — these exercise the persisted-name → Color lookup and the
 * palette invariants only.
 */
class AccentPaletteTest {

    @Test
    fun teal_resolves_to_the_default() {
        // The persisted default name must round-trip to the canonical TealDefault accent.
        assertEquals(TealDefault, accentColorFor("teal"))
    }

    @Test
    fun blue_resolves_to_its_hex() {
        assertEquals(Color(0xFF60A5FA), accentColorFor("blue"))
    }

    @Test
    fun unknown_name_falls_back_to_teal() {
        assertEquals(TealDefault, accentColorFor("nonsense"))
    }

    @Test
    fun blank_name_falls_back_to_teal() {
        assertEquals(TealDefault, accentColorFor(""))
    }

    @Test
    fun palette_has_twenty_presets() {
        assertEquals(20, ACCENT_PRESETS.size)
    }

    @Test
    fun palette_names_are_unique() {
        val names = ACCENT_PRESETS.map { it.name }
        assertEquals(names.size, names.toSet().size)
    }

    @Test
    fun palette_contains_teal() {
        assertTrue(ACCENT_PRESETS.any { it.name == "teal" })
    }

    @Test
    fun teal_preset_color_equals_default() {
        // The "teal" preset's own color must equal TealDefault (the default choice round-trips).
        val teal = ACCENT_PRESETS.first { it.name == "teal" }
        assertEquals(TealDefault, teal.color)
    }
}
