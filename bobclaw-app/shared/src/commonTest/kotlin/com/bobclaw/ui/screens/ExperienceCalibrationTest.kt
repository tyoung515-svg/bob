package com.bobclaw.ui.screens

import com.bobclaw.model.Capabilities
import com.bobclaw.model.Face
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Pure-logic tests for the U9 Simple/Pro calibration — the load-bearing fence. Proves without any UI:
 *   1. **Pro renders byte-identical to pre-U9** (the Pro label/flag helpers reproduce the old formulas),
 *   2. the Simple **mode picker is driven off `Face.simpleSlot`** with no hardcoded face map, and
 *   3. the app **Face model parses** the U2 display metadata (display_name/blurb/simple_slot).
 */
class ExperienceCalibrationTest {

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private fun face(
        id: String,
        name: String = id,
        displayName: String? = null,
        blurb: String? = null,
        simpleSlot: String? = null,
    ) = Face(
        id = id,
        name = name,
        avatar = "$id.png",
        preferredBackend = "local",
        uiTheme = "dark",
        displayName = displayName,
        blurb = blurb,
        simpleSlot = simpleSlot,
    )

    // ── the single knob ───────────────────────────────────────────────────────────
    @Test
    fun pro_is_exactly_pro_everything_else_is_simple() {
        assertTrue(isProExperience(EXPERIENCE_PRO))
        assertFalse(isProExperience(EXPERIENCE_SIMPLE))
        // default-simple posture: an unexpected value renders Simple (the safe default), never Pro
        assertFalse(isProExperience("garbage"))
        assertTrue(isSimpleExperience(EXPERIENCE_SIMPLE))
        assertTrue(isSimpleExperience("garbage"))
        assertFalse(isSimpleExperience(EXPERIENCE_PRO))
    }

    // ── Pro byte-identical: label helpers reproduce the pre-U9 formulas ────────────
    @Test
    fun pro_face_label_is_the_raw_name_verbatim() {
        val f = face("planner-minimax", name = "planner-minimax", displayName = "Deep Thinker")
        // Pre-U9 chip read face.name — Pro MUST return exactly that, never the friendly name.
        assertEquals("planner-minimax", faceChipLabel(f, EXPERIENCE_PRO))
        // Simple swaps in the friendly display_name.
        assertEquals("Deep Thinker", faceChipLabel(f, EXPERIENCE_SIMPLE))
    }

    @Test
    fun pro_label_or_id_matches_pre_u9_formula() {
        val f = face("assistant", name = "assistant", displayName = "Everyday Assistant")
        // Pre-U9: faces.firstOrNull{...}?.name ?: selectedFaceId
        assertEquals("assistant", faceLabelOrId(f, "assistant", EXPERIENCE_PRO))
        assertEquals("assistant", faceLabelOrId(null, "assistant", EXPERIENCE_PRO)) // no face record → id
        // Simple: friendly label, still id-fallback when no record.
        assertEquals("Everyday Assistant", faceLabelOrId(f, "assistant", EXPERIENCE_SIMPLE))
        assertEquals("mystery-id", faceLabelOrId(null, "mystery-id", EXPERIENCE_SIMPLE))
    }

    @Test
    fun simple_label_falls_back_to_name_when_display_name_absent_or_blank() {
        assertEquals("raw-name", faceChipLabel(face("x", name = "raw-name", displayName = null), EXPERIENCE_SIMPLE))
        assertEquals("raw-name", faceChipLabel(face("x", name = "raw-name", displayName = "  "), EXPERIENCE_SIMPLE))
    }

    // ── humanizeSlot: pure transform, NOT a hardcoded label map ────────────────────
    @Test
    fun humanize_slot_sentence_cases_underscored_values() {
        assertEquals("Quick", humanizeSlot("quick"))
        assertEquals("Think hard", humanizeSlot("think_hard"))
        assertEquals("Team of experts", humanizeSlot("team_of_experts"))
        // an unseen slot value is still humanized deterministically (no code needed for a 4th mode)
        assertEquals("Wizard mode", humanizeSlot("wizard_mode"))
    }

    // ── simpleModes: driven ONLY by simple_slot, no hardcoded faceId map ───────────
    @Test
    fun modes_come_only_from_faces_with_a_simple_slot() {
        val faces = listOf(
            face("planner-claude"),                              // no slot → excluded
            face("assistant", simpleSlot = "quick", displayName = "Everyday Assistant", blurb = "Fast."),
            face("planner-minimax", simpleSlot = "think_hard"),
            face("council-max", simpleSlot = "team_of_experts"),
            face("worker-1"),                                    // no slot → excluded
        )
        val modes = simpleModes(faces)
        assertEquals(3, modes.size)
        // canonical order (cheap → heavy), regardless of face list order
        assertEquals(listOf("quick", "think_hard", "team_of_experts"), modes.map { it.slot })
        assertEquals(listOf("assistant", "planner-minimax", "council-max"), modes.map { it.faceId })
        assertEquals(listOf("Quick", "Think hard", "Team of experts"), modes.map { it.label })
        // friendly copy carried through
        assertEquals("Everyday Assistant", modes.first().displayName)
        assertEquals("Fast.", modes.first().blurb)
    }

    @Test
    fun modes_are_reordered_by_canonical_slot_not_by_face_order() {
        // Faces supplied heavy → cheap; simpleModes must still emit cheap → heavy.
        val faces = listOf(
            face("c", simpleSlot = "team_of_experts"),
            face("b", simpleSlot = "think_hard"),
            face("a", simpleSlot = "quick"),
        )
        assertEquals(listOf("quick", "think_hard", "team_of_experts"), simpleModes(faces).map { it.slot })
    }

    @Test
    fun an_unknown_slot_still_appears_proving_no_hardcoded_allowlist() {
        // A data-only 4th mode (new simple_slot on any face) must surface with zero code changes,
        // appended after the known slots in stable face order — proving there is no hardcoded map.
        val faces = listOf(
            face("z", simpleSlot = "wizard_mode"),
            face("a", simpleSlot = "quick"),
        )
        val modes = simpleModes(faces)
        assertEquals(listOf("quick", "wizard_mode"), modes.map { it.slot })
        assertEquals("Wizard mode", modes.last().label)
        // The mode's faceId is purely the data's — no app-side face→mode wiring.
        assertEquals("z", modes.last().faceId)
    }

    @Test
    fun duplicate_slots_dedupe_first_face_wins() {
        val faces = listOf(
            face("primary", simpleSlot = "quick"),
            face("shadow", simpleSlot = "quick"),
        )
        val modes = simpleModes(faces)
        assertEquals(1, modes.size)
        assertEquals("primary", modes.first().faceId)
    }

    @Test
    fun blank_or_null_simple_slot_never_becomes_a_mode() {
        val faces = listOf(face("a", simpleSlot = ""), face("b", simpleSlot = "  "), face("c", simpleSlot = null))
        assertTrue(simpleModes(faces).isEmpty())
    }

    // ── sweep visibility flags (Pro = today's surface) ─────────────────────────────
    @Test
    fun sweep_flags_split_simple_and_pro_correctly() {
        // Pro: power chips inline, full face dropdown, technical placeholder, routing tab visible.
        assertTrue(showPowerChipsInline(EXPERIENCE_PRO))
        assertFalse(useModePicker(EXPERIENCE_PRO))
        assertFalse(useSimplePlaceholder(EXPERIENCE_PRO))
        assertTrue(showResolvedRoutingTab(EXPERIENCE_PRO))
        // Simple: power chips collapsed, mode picker, "Message Bob…" placeholder, routing tab hidden.
        assertFalse(showPowerChipsInline(EXPERIENCE_SIMPLE))
        assertTrue(useModePicker(EXPERIENCE_SIMPLE))
        assertTrue(useSimplePlaceholder(EXPERIENCE_SIMPLE))
        assertFalse(showResolvedRoutingTab(EXPERIENCE_SIMPLE))
    }

    // ── verify-finding #1: the app Face model parses the U2 display metadata ────────
    @Test
    fun face_model_parses_display_name_blurb_and_simple_slot_from_capabilities() {
        val payload = """
            {"faces":[
              {"id":"assistant","name":"assistant","avatar":"a.png","preferred_backend":"local",
               "ui_theme":"dark","display_name":"Everyday Assistant","blurb":"Fast everyday help.",
               "simple_slot":"quick"}
            ]}
        """.trimIndent()
        val caps = json.decodeFromString(Capabilities.serializer(), payload)
        val f = caps.faces.single()
        assertEquals("Everyday Assistant", f.displayName)
        assertEquals("Fast everyday help.", f.blurb)
        assertEquals("quick", f.simpleSlot)
    }

    @Test
    fun face_model_defaults_metadata_to_null_when_absent() {
        // An older gateway that omits the three fields ⇒ null ⇒ id/name fallback, zero breakage.
        val payload = """{"id":"x","name":"x","avatar":"x.png","preferred_backend":"local","ui_theme":"dark"}"""
        val f = json.decodeFromString(Face.serializer(), payload)
        assertNull(f.displayName)
        assertNull(f.blurb)
        assertNull(f.simpleSlot)
        // and it produces no Simple mode
        assertTrue(simpleModes(listOf(f)).isEmpty())
    }

    // ── R0: GPT-5.6 roster — Sol/Terra/Luna are distinguishable per face ─────────────
    @Test
    fun gpt56_roster_faces_are_distinguishable_per_face() {
        val sol = face(
            id = "planner-gpt56-sol",
            name = "Planner (GPT-5.6 Sol)",
            displayName = "GPT-5.6 Sol (Conductor)",
        )
        val terra = face(
            id = "worker-gpt56-terra",
            name = "Worker (GPT-5.6 Terra)",
            displayName = "GPT-5.6 Terra (Primary Builder)",
        )
        val luna = face(
            id = "worker-gpt56-luna",
            name = "Worker (GPT-5.6 Luna)",
            displayName = "GPT-5.6 Luna (Parallel Builder)",
        )
        val faces = listOf(sol, terra, luna)

        assertEquals(3, faces.map { faceChipLabel(it, EXPERIENCE_PRO) }.toSet().size)
        assertEquals(3, faces.map { faceChipLabel(it, EXPERIENCE_SIMPLE) }.toSet().size)
        assertEquals("Worker (GPT-5.6 Terra)", faceChipLabel(terra, EXPERIENCE_PRO))
        assertEquals(
            "GPT-5.6 Terra (Primary Builder)",
            faceChipLabel(terra, EXPERIENCE_SIMPLE),
        )
        assertTrue(simpleModes(faces).isEmpty())
    }
}
