package com.bobclaw.ui.components

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Headless guard for the bundled Tabler [IconGlyph] subset. Building each [tablerIcon] runs its path
 * data through `PathParser` — so a malformed embedded path string fails HERE (under `./gradlew test`),
 * not at render time on the screenshot pass.
 */
class IconGlyphTest {

    @Test
    fun every_known_icon_parses_and_builds() {
        for (name in KNOWN_ICON_NAMES) {
            val vector = tablerIcon(name)
            // The builder stamps the name; a fallback would carry "point-filled" instead.
            assertEquals(name, vector.name, "icon '$name' did not build to its own vector")
            assertTrue(vector.defaultWidth.value > 0f && vector.defaultHeight.value > 0f)
        }
    }

    @Test
    fun unknown_icon_falls_back_to_point_filled() {
        assertEquals("point-filled", tablerIcon("no-such-icon").name)
        assertEquals(false, isKnownIcon("no-such-icon"))
    }

    @Test
    fun subset_covers_the_manifest_names() {
        // ASSET-MANIFEST §1 — the 12 enumerated icons (+ "x" for the approvals denied mark).
        for (name in listOf(
            "message-circle", "scale", "users", "arrows-split", "checks", "layout-dashboard",
            "settings", "power", "point-filled", "tool", "arrow-bounce", "satellite",
        )) {
            assertTrue(isKnownIcon(name), "manifest icon '$name' missing from the subset")
        }
        assertTrue(isKnownIcon("x"))
        // MS9 U1 (IA restructure) adds three: "home" (Home nav), "brain" (Memory nav), "clock"
        // (scheduled-fires tile). Total = 13 + 3 = 16.
        for (name in listOf("home", "brain", "clock")) {
            assertTrue(isKnownIcon(name), "U1 icon '$name' missing from the subset")
        }
        assertEquals(16, KNOWN_ICON_NAMES.size)
    }
}
