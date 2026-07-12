package com.bobclaw.ui.screens

import com.bobclaw.model.Face

/**
 * Pure, Compose-free Simple/Pro calibration logic (SPEC §6/§7, MS9-U9). This is the **load-bearing
 * fence**: every presentation decision the U9 sweep makes lives here so `commonTest` can prove, with
 * no UI, both invariants —
 *   1. **Pro renders byte-identical to pre-U9** ([isProExperience] gates the unchanged path; the
 *      Pro label helpers reproduce the exact pre-U9 formulas), and
 *   2. the Simple **mode picker is driven entirely off [Face.simpleSlot]** with **no hardcoded
 *      app-side faceId→mode map** ([simpleModes] reads only the data).
 *
 * Reuses [EXPERIENCE_SIMPLE] / [EXPERIENCE_PRO] from `ApprovalLiteracy.kt` (same package) — U9 EXTENDS
 * the single `UserPrefs.experienceLevel` pref U6 introduced; it does not add a parallel one.
 *
 * Fence: presentation ONLY. Nothing here changes backend/routing behavior — the Simple picker sets
 * the same `selectedFaceId` pin the Pro face dropdown does; it just presents it in plain language.
 */

// ── The single knob ─────────────────────────────────────────────────────────────────────────────
/** True for the Pro surface: EXACTLY [EXPERIENCE_PRO]. Everything else — the default `simple` and any
 *  out-of-range value the codec coerces to `simple` — renders Simple, the safe default. Every U9
 *  conditional branches on this one predicate so the Pro branch stays the pre-U9 render verbatim. */
fun isProExperience(level: String): Boolean = level == EXPERIENCE_PRO

/** Complement of [isProExperience]: Simple is the default for anything not explicitly `pro`. */
fun isSimpleExperience(level: String): Boolean = !isProExperience(level)

// ── Friendly face labels (SPEC §6 "friendly display_name/blurb everywhere") ──────────────────────
/**
 * The face label for a chip/menu. **Pro returns [Face.name] verbatim** — byte-identical to the pre-U9
 * chip, which read `face.name`. Simple prefers the friendly [Face.displayName] (U2) and falls back to
 * the raw name when it is absent/blank (older gateway ⇒ null ⇒ never a blank chip).
 */
fun faceChipLabel(face: Face, level: String): String =
    if (isProExperience(level)) face.name
    else face.displayName?.takeIf { it.isNotBlank() } ?: face.name

/**
 * The pre-U9 chat face-pill formula, calibrated. The old code was
 * `faces.firstOrNull { it.id == selectedFaceId }?.name ?: selectedFaceId`; in **Pro** this returns
 * exactly that (`face?.name ?: id`). In Simple it swaps in the friendly label, still falling back to
 * the id when no face record is loaded. This equivalence is the Pro-byte-identical proof.
 */
fun faceLabelOrId(face: Face?, faceId: String, level: String): String =
    face?.let { faceChipLabel(it, level) } ?: faceId

// ── Simple-mode picker (SPEC §6 — driven by simple_slot, NO hardcoded map) ───────────────────────
/**
 * One plain-language Simple-mode entry. Derived ENTIRELY from a [Face] that carries a non-blank
 * [Face.simpleSlot]; there is no app-side faceId→mode table. [faceId] is the pin the picker applies
 * (the same pin the Pro dropdown sets); [slot] is the server value; [label] is the humanized slot;
 * [displayName]/[blurb] are the face's friendly copy (U2) for a subtitle/tooltip.
 */
data class SimpleMode(
    val faceId: String,
    val slot: String,
    val label: String,
    val displayName: String?,
    val blurb: String?,
)

/**
 * Canonical DISPLAY ORDER for the known slots (cheap → heavy). This is an ordering hint ONLY: it is
 * NOT a face map, and it is NOT an allow-list — a slot absent here (a data-only 4th mode added later
 * by putting a `simple_slot` on one more face, per U2's note) still appears, appended after the known
 * ones in stable face order. Changing it never changes WHICH faces are modes, only their order.
 */
val SIMPLE_SLOT_ORDER: List<String> = listOf("quick", "think_hard", "team_of_experts")

/**
 * Humanize a raw `simple_slot` into a picker label: `think_hard` → "Think hard",
 * `team_of_experts` → "Team of experts", `quick` → "Quick". A pure string transform (underscores →
 * spaces, sentence case) that is deterministic for ANY slot value — so a new slot needs zero code.
 * This is NOT a hardcoded label map.
 */
fun humanizeSlot(slot: String): String {
    val words = slot.split('_').map { it.trim() }.filter { it.isNotEmpty() }
    if (words.isEmpty()) return slot
    val head = words.first().replaceFirstChar { it.uppercase() }
    val tail = words.drop(1).joinToString(" ")
    return if (tail.isEmpty()) head else "$head $tail"
}

/**
 * Build the Simple-mode picker rows from the live faces: exactly those whose [Face.simpleSlot] is
 * non-blank, de-duplicated by slot (first face wins so a stable identity backs each mode), ordered
 * by [SIMPLE_SLOT_ORDER] then stable face order for unknown slots. Purely data-driven — zero
 * hardcoded face ids. Empty ⇒ the caller falls back to the full face list, never a dead UI.
 */
fun simpleModes(faces: List<Face>): List<SimpleMode> {
    val seen = HashSet<String>()
    val rows = ArrayList<SimpleMode>()
    for (f in faces) {
        val slot = f.simpleSlot?.trim()?.takeIf { it.isNotEmpty() } ?: continue
        if (!seen.add(slot)) continue
        rows.add(
            SimpleMode(
                faceId = f.id,
                slot = slot,
                label = humanizeSlot(slot),
                displayName = f.displayName?.takeIf { it.isNotBlank() },
                blurb = f.blurb?.takeIf { it.isNotBlank() },
            )
        )
    }
    // Stable sort: known slots by canonical index, unknown slots keep their (post-known) face order.
    return rows.sortedBy { m ->
        val i = SIMPLE_SLOT_ORDER.indexOf(m.slot)
        if (i >= 0) i else SIMPLE_SLOT_ORDER.size
    }
}

// ── Sweep visibility flags (chat chips/placeholder · Teams · routing tab) ────────────────────────
/**
 * Whether the jargon-y **power chips** (the backend picker showing ids like `deepseek_v4_flash`, and
 * the profile picker) render INLINE. Pro: yes (today's surface). Simple: no — collapsed behind a
 * "Details" affordance (§6). In Simple they are shown only when the user expands Details.
 */
fun showPowerChipsInline(level: String): Boolean = isProExperience(level)

/** Whether the chat face selector is the plain 3-way mode picker (Simple) vs the full face dropdown
 *  (Pro, unchanged). */
fun useModePicker(level: String): Boolean = isSimpleExperience(level)

/** Whether the composer shows the Simple "Message Bob…" placeholder (Simple) vs today's
 *  face-named placeholder (Pro, unchanged). */
fun useSimplePlaceholder(level: String): Boolean = isSimpleExperience(level)

/**
 * Teams **"Resolved routing"** tab visibility. Pro-only (SPEC §2: "Pro-visibility once U9 lands").
 * Simple hides the debug routing table AND its jargon tab strip, leaving the plain team builder.
 */
fun showResolvedRoutingTab(level: String): Boolean = isProExperience(level)
