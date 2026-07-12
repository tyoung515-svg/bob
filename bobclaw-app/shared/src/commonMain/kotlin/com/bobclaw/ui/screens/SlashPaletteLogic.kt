package com.bobclaw.ui.screens

import com.bobclaw.model.Capabilities

/**
 * The three display groups for a slash-palette entry. Declaration order = display group order:
 * actions first, then faces, then backends. (Authored by the DeepSeek worker tier per the A2
 * contract; the Compose overlay in SlashPalette.kt renders this pure model.)
 */
internal enum class PaletteKind {
    ACTION,
    FACE,
    BACKEND
}

/**
 * A single entry in the chat `/` slash palette.
 *
 * @property kind      the palette group this entry belongs to
 * @property id        stable value acted upon by the caller (action id / face id / backend name)
 * @property label     primary display text (also used as the main filter key)
 * @property detail    secondary text shown alongside the label
 * @property available false only for an unavailable backend; drives dimmed rendering
 */
internal data class PaletteEntry(
    val kind: PaletteKind,
    val id: String,
    val label: String,
    val detail: String,
    val available: Boolean = true
)

/**
 * Builds the ordered list of [PaletteEntry] from the live [caps] capabilities document (GET
 * /capabilities, MS8-G1). Always returns the single ACTION entry "init" first, then one FACE entry
 * per face (in order), then one BACKEND entry per backend (in order). If [caps] is `null` (registry
 * not loaded yet or the fetch failed) only the ACTION entry is returned.
 */
internal fun buildPaletteEntries(caps: Capabilities?): List<PaletteEntry> {
    val entries = mutableListOf<PaletteEntry>()

    // 1. ACTION entry (always present, even when caps is null)
    entries.add(
        PaletteEntry(
            kind = PaletteKind.ACTION,
            id = "init",
            label = "init",
            detail = "Kick off a starter build"
        )
    )

    if (caps != null) {
        // 2. FACE entries in original order
        for (face in caps.faces) {
            entries.add(
                PaletteEntry(
                    kind = PaletteKind.FACE,
                    id = face.id,
                    label = face.name,
                    detail = face.preferredBackend
                )
            )
        }

        // 3. BACKEND entries in original order
        for (backend in caps.backends) {
            entries.add(
                PaletteEntry(
                    kind = PaletteKind.BACKEND,
                    id = backend.backend,
                    label = backend.backend,
                    detail = backend.model ?: (if (backend.available) "available" else "unavailable"),
                    available = backend.available
                )
            )
        }
    }

    return entries
}

/**
 * Filters [entries] to those matching [query] (case-insensitive). Trims + lowercases the query; a
 * blank query returns [entries] unchanged. Otherwise keeps entries whose lowercased `label`, `id`,
 * `detail`, or `kind.name` CONTAINS the query as a substring. Input order is preserved (stable).
 */
internal fun filterPaletteEntries(
    entries: List<PaletteEntry>,
    query: String
): List<PaletteEntry> {
    val trimmed = query.trim().lowercase()
    if (trimmed.isEmpty()) return entries

    return entries.filter { entry ->
        entry.label.lowercase().contains(trimmed) ||
                entry.id.lowercase().contains(trimmed) ||
                entry.detail.lowercase().contains(trimmed) ||
                entry.kind.name.lowercase().contains(trimmed)
    }
}
