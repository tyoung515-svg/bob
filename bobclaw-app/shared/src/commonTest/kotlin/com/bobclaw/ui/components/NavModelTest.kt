package com.bobclaw.ui.components

import com.bobclaw.ui.RailDest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Headless guard for the U1 IA restructure (SPEC §2 / D1). Asserts the rail's STRUCTURE — the
 * destination order + that Routing's top-level entry is gone — under `:shared:jvmTest`, before the
 * attended screenshot pass. Labels localize at render (visual), so they are not asserted here.
 */
class NavModelTest {

    private val d1Order = listOf(
        RailDest.HOME, RailDest.CHAT, RailDest.COUNCIL,
        RailDest.TEAMS, RailDest.MEMORY, RailDest.APPROVALS,
    )

    @Test
    fun rail_order_matches_D1_home_first() {
        assertEquals(d1Order, RAIL_ITEMS.map { it.dest })
        assertEquals(RailDest.HOME, RAIL_ITEMS.first().dest, "Home must be the first/landing rail item")
    }

    @Test
    fun routing_dashboard_settings_logout_are_not_rail_destinations() {
        // Routing's top-level nav DIES in U1 (moved into a Teams tab); Dashboard was renamed Home;
        // Settings + Logout are footer actions, never top-rail destinations. The enum proves it.
        val names = RailDest.values().map { it.name }.toSet()
        assertFalse("ROUTING" in names, "Routing must not be a rail destination")
        assertFalse("DASHBOARD" in names, "Dashboard was promoted to Home")
        assertFalse("SETTINGS" in names)
        assertFalse("LOGOUT" in names)
    }

    @Test
    fun memory_is_wired_as_a_rail_destination() {
        assertTrue(RAIL_ITEMS.any { it.dest == RailDest.MEMORY }, "Memory nav entry must exist (U1)")
    }

    @Test
    fun every_rail_icon_is_a_bundled_glyph_not_the_fallback() {
        RAIL_ITEMS.forEach {
            assertTrue(isKnownIcon(it.iconName), "rail icon '${it.iconName}' is not a bundled glyph")
        }
    }

    @Test
    fun no_duplicate_rail_destinations() {
        assertEquals(RAIL_ITEMS.size, RAIL_ITEMS.map { it.dest }.toSet().size)
    }
}
