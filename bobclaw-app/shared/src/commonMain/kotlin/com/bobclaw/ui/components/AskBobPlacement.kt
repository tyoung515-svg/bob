package com.bobclaw.ui.components

/**
 * MS9-UD — where the "Ask Bob" helper mounts for a given surface.
 *
 * The U5 bubble FLOATS at the bottom-right over Compose. But some screens host a heavyweight JCEF
 * interop canvas (Memory's 3D graph) that paints ABOVE all Compose (interop z-order), so a floating
 * bubble is fully occluded there. On such canvas pages Ask Bob is instead DOCKED as a right-side
 * panel that shrinks the canvas (mirroring the Memory Inspect panel), so it is never painted over.
 */
enum class AskBobPlacement {
    /** Floats bottom-right over the page (the U5 default for ordinary Compose pages). */
    FLOATING,
    /** Docked as a shrinking right-side panel beside a heavyweight canvas (Memory). */
    DOCKED,
}

/**
 * Surface ids whose page is a heavyweight interop canvas → Ask Bob must DOCK, never float (else it
 * is occluded by the canvas). Memory is the only such page today; future JCEF-canvas pages add here.
 */
internal val CANVAS_DOCK_PAGES = setOf("memory")

/**
 * Decide where (if anywhere) Ask Bob mounts for the resolved surface id [page].
 *
 * Pure routing (no Compose) so the floating-vs-docked decision is unit-tested rather than buried in
 * `App.kt`'s branch (MS9-UD verify #3):
 *   - blank ⇒ `null` — Chat (and any surface that opts out) gets NO Ask Bob.
 *   - a canvas page (e.g. "memory") ⇒ [AskBobPlacement.DOCKED] — a floating bubble would be occluded.
 *   - anything else ⇒ [AskBobPlacement.FLOATING] — the U5 default.
 *
 * `App.kt` renders the floating bubble ONLY for [AskBobPlacement.FLOATING]; a DOCKED page renders its
 * own dock inside the screen (so the canvas can shrink around it), and `null` renders nothing.
 */
fun askBobPlacement(page: String): AskBobPlacement? {
    val p = page.trim()
    if (p.isEmpty()) return null
    if (p.lowercase() in CANVAS_DOCK_PAGES) return AskBobPlacement.DOCKED
    return AskBobPlacement.FLOATING
}
