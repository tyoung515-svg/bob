package com.bobclaw.ui.components

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * MS9-UD — the pure floating-vs-docked routing (verify #3). Proves the App.kt invariant WITHOUT the
 * Compose tree: Memory (a heavyweight-canvas page) DOCKS, Chat gets NO Ask Bob, and every other page
 * FLOATS unchanged. This is the single source of truth `App.kt` branches on.
 */
class AskBobPlacementTest {

    @Test
    fun chat_or_blank_gets_no_ask_bob() {
        // Chat resolves to "" in App.kt (it IS the chat) → no Ask Bob at all.
        assertNull(askBobPlacement(""))
        assertNull(askBobPlacement("   "))
    }

    @Test
    fun memory_canvas_page_docks() {
        // Memory's JCEF canvas would occlude a floating bubble → DOCKED (case-insensitive).
        assertEquals(AskBobPlacement.DOCKED, askBobPlacement("memory"))
        assertEquals(AskBobPlacement.DOCKED, askBobPlacement("MEMORY"))
        assertEquals(AskBobPlacement.DOCKED, askBobPlacement("  memory  "))
    }

    @Test
    fun ordinary_pages_float_unchanged() {
        // Every non-canvas, non-chat surface keeps the U5 floating bubble.
        for (page in listOf("home", "teams", "approvals", "council", "settings")) {
            assertEquals(AskBobPlacement.FLOATING, askBobPlacement(page), "page=$page")
        }
    }

    @Test
    fun each_surface_routes_to_exactly_one_placement() {
        // The routing invariant App.kt relies on: Memory is the ONLY docked surface today, Chat the
        // ONLY none surface, and the rest float — so App renders the floating bubble on precisely
        // the FLOATING set and the Memory dock on precisely the DOCKED set (no double-mount).
        assertTrue(listOf("memory").all { askBobPlacement(it) == AskBobPlacement.DOCKED })
        assertTrue(listOf("").all { askBobPlacement(it) == null })
        assertTrue(
            listOf("home", "teams", "approvals", "council", "settings")
                .all { askBobPlacement(it) == AskBobPlacement.FLOATING },
        )
    }
}
