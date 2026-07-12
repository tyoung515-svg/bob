package com.bobclaw.ui.components

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * U11 voice affordances — the pure gating + intent-seam logic. These prove the two accept invariants
 * at the logic layer (the visuals are the screenshot gate):
 *   1. **flag OFF ⇒ nothing renders** ([voiceAffordancesVisible] is false for the off/default flag),
 *   2. the **intent→action seam names EVERY U3 registry seed action** (seam-doc completeness).
 * Also pins the "no engine ⇒ disabled" behaviour that keeps the affordances inert in v1.
 */
class VoiceAffordancesTest {

    // ── invariant 1: flag-off byte-identical (nothing new emitted) ─────────────────────────────
    @Test
    fun voice_affordances_hidden_when_flag_off() {
        // The whole point: OFF ⇒ the guarded `if (...) { mic }` blocks are skipped ⇒ byte-identical.
        assertFalse(voiceAffordancesVisible(false))
    }

    @Test
    fun voice_affordances_shown_when_flag_on() {
        assertTrue(voiceAffordancesVisible(true))
    }

    // ── inert-but-present: no speech engine in v1 ⇒ disabled ───────────────────────────────────
    @Test
    fun no_speech_engine_in_v1() {
        // SPEC §7: "NO STT/TTS engine in v1; the seam is the deliverable."
        assertFalse(SPEECH_ENGINE_PRESENT)
    }

    @Test
    fun mic_and_read_aloud_disabled_without_engine() {
        assertFalse(micEnabled(false))
        assertFalse(readAloudEnabled(false))
        // The default (no engine wired) is disabled.
        assertFalse(micEnabled())
        assertFalse(readAloudEnabled())
        // …and both would enable the day an engine is present (no other code change needed).
        assertTrue(micEnabled(true))
        assertTrue(readAloudEnabled(true))
    }

    // ── invariant 2: intent→action seam names EVERY registry seed action ───────────────────────
    @Test
    fun seed_action_ids_match_core_registry() {
        // Verbatim from core core/actions/registry.py::SEED_ACTIONS (the eight seed ids).
        assertEquals(
            listOf(
                "create_team", "delete_team", "pin_face", "switch_profile",
                "forget_fact", "new_conversation", "approve", "deny",
            ),
            SEED_ACTION_IDS,
        )
    }

    @Test
    fun every_seed_action_has_a_voice_intent() {
        // Seam-doc completeness: no seed action is left without a voice intent phrase.
        assertEquals(emptyList(), missingVoiceIntents())
        for (id in SEED_ACTION_IDS) {
            val phrase = voiceIntentFor(id)
            assertTrue(phrase != null && phrase.isNotBlank(), "no voice intent for '$id'")
        }
    }

    @Test
    fun voice_intent_lookup_is_null_for_unknown_action() {
        assertNull(voiceIntentFor("no_such_action"))
        assertNull(voiceIntentFor(""))
    }

    @Test
    fun missing_voice_intents_detects_a_gap() {
        // If a future seed id were added without an intent, missingVoiceIntents() would flag it.
        assertEquals(listOf("brand_new_action"), missingVoiceIntents(listOf("create_team", "brand_new_action")))
    }
}
