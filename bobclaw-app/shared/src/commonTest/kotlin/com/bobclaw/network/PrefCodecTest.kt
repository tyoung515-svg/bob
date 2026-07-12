package com.bobclaw.network

import kotlin.test.Test
import kotlin.test.assertEquals

class PrefCodecTest {

    @Test
    fun round_trip_encode_decode_equals_original() {
        val prefs = UserPrefs(uiScale = 1.25f, accentName = "amber", theme = "light", density = "compact")
        assertEquals(prefs, PrefCodec.decode(PrefCodec.encode(prefs)))
    }

    @Test
    fun round_trip_defaults() {
        val prefs = UserPrefs()
        assertEquals(prefs, PrefCodec.decode(PrefCodec.encode(prefs)))
    }

    @Test
    fun blank_input_decodes_to_defaults() {
        assertEquals(UserPrefs(), PrefCodec.decode(""))
        assertEquals(UserPrefs(), PrefCodec.decode("   \n\n  \t \n"))
    }

    @Test
    fun unknown_keys_are_ignored() {
        val text = "uiScale=1.2\nbogus=whatever\nmysteryKey=42\naccentName=violet\n"
        val decoded = PrefCodec.decode(text)
        assertEquals(1.2f, decoded.uiScale)
        assertEquals("violet", decoded.accentName)
        // untouched keys keep their defaults
        assertEquals("dark", decoded.theme)
        assertEquals("comfortable", decoded.density)
    }

    @Test
    fun missing_keys_fall_back_to_defaults() {
        // only uiScale present; the rest must default
        val decoded = PrefCodec.decode("uiScale=1.1\n")
        assertEquals(1.1f, decoded.uiScale)
        assertEquals("teal", decoded.accentName)
        assertEquals("dark", decoded.theme)
        assertEquals("comfortable", decoded.density)
    }

    @Test
    fun uiScale_above_range_clamps_to_max() {
        assertEquals(UI_SCALE_MAX, PrefCodec.decode("uiScale=2.0\n").uiScale)
    }

    @Test
    fun uiScale_below_range_clamps_to_min() {
        assertEquals(UI_SCALE_MIN, PrefCodec.decode("uiScale=0.1\n").uiScale)
    }

    @Test
    fun malformed_uiScale_float_falls_back_to_default() {
        assertEquals(1.0f, PrefCodec.decode("uiScale=not-a-number\n").uiScale)
        assertEquals(1.0f, PrefCodec.decode("uiScale=\n").uiScale)
    }

    @Test
    fun lines_without_separator_are_skipped() {
        // a stray non-key=value line must not corrupt parsing
        val decoded = PrefCodec.decode("garbage line with no equals\nuiScale=1.3\n")
        assertEquals(1.3f, decoded.uiScale)
    }

    @Test
    fun blank_accent_falls_back_to_default() {
        // an explicit blank value must not blank out the field
        assertEquals("teal", PrefCodec.decode("accentName=\n").accentName)
    }

    @Test
    fun valid_theme_values_round_trip() {
        for (t in THEME_OPTIONS) {
            assertEquals(t, PrefCodec.decode("theme=$t\n").theme)
        }
    }

    @Test
    fun invalid_theme_falls_back_to_default() {
        // a corrupt/unknown theme must not leak into the app (A1 toggle only knows dark|light|system)
        assertEquals("dark", PrefCodec.decode("theme=neon\n").theme)
        assertEquals("dark", PrefCodec.decode("theme=\n").theme)
    }

    // ── U5 confirm-once persistence (D12 guardrail) ────────────────────────────
    @Test
    fun confirmed_actions_round_trip() {
        val prefs = UserPrefs(confirmedActions = setOf("create_team", "delete_team"))
        assertEquals(prefs, PrefCodec.decode(PrefCodec.encode(prefs)))
    }

    @Test
    fun confirmed_actions_default_empty_and_blank_line_decodes_empty() {
        assertEquals(emptySet(), PrefCodec.decode("").confirmedActions)
        assertEquals(emptySet(), PrefCodec.decode("confirmedActions=\n").confirmedActions)
    }

    @Test
    fun confirmed_actions_ignores_blank_entries() {
        assertEquals(
            setOf("create_team", "pin_face"),
            PrefCodec.decode("confirmedActions=create_team, ,pin_face,\n").confirmedActions,
        )
    }

    // ── U6 experience_level (SPEC §6; U9 extends this same pref) ────────────────
    @Test
    fun experience_level_round_trips_both_values() {
        for (level in EXPERIENCE_LEVELS) {
            assertEquals(level, PrefCodec.decode("experienceLevel=$level\n").experienceLevel)
        }
        assertEquals(
            UserPrefs(experienceLevel = "pro"),
            PrefCodec.decode(PrefCodec.encode(UserPrefs(experienceLevel = "pro"))),
        )
    }

    @Test
    fun experience_level_defaults_to_simple() {
        // absent key (back-compat: an old prefs file written before U6) → simple
        assertEquals("simple", PrefCodec.decode("").experienceLevel)
        assertEquals("simple", PrefCodec.decode("uiScale=1.0\n").experienceLevel)
        assertEquals("simple", UserPrefs().experienceLevel)
    }

    @Test
    fun invalid_experience_level_falls_back_to_simple() {
        assertEquals("simple", PrefCodec.decode("experienceLevel=wizard\n").experienceLevel)
        assertEquals("simple", PrefCodec.decode("experienceLevel=\n").experienceLevel)
    }

    // ── U11 voice_beta preview flag (SPEC §7) — flag-off byte-identical starts at persistence ──
    @Test
    fun voice_beta_defaults_off() {
        // The default is the byte-identical-UI state; an old prefs file (no key) must stay OFF.
        assertEquals(false, UserPrefs().voiceBeta)
        assertEquals(false, PrefCodec.decode("").voiceBeta)
        assertEquals(false, PrefCodec.decode("uiScale=1.0\n").voiceBeta)
    }

    @Test
    fun voice_beta_round_trips_both_values() {
        assertEquals(UserPrefs(voiceBeta = true), PrefCodec.decode(PrefCodec.encode(UserPrefs(voiceBeta = true))))
        assertEquals(UserPrefs(voiceBeta = false), PrefCodec.decode(PrefCodec.encode(UserPrefs(voiceBeta = false))))
        assertEquals(true, PrefCodec.decode("voiceBeta=true\n").voiceBeta)
        assertEquals(false, PrefCodec.decode("voiceBeta=false\n").voiceBeta)
    }

    @Test
    fun voice_beta_garbage_or_blank_stays_off() {
        // Only an explicit "true" enables the preview; anything else is the safe OFF default.
        assertEquals(false, PrefCodec.decode("voiceBeta=\n").voiceBeta)
        assertEquals(false, PrefCodec.decode("voiceBeta=yes\n").voiceBeta)
        assertEquals(false, PrefCodec.decode("voiceBeta=1\n").voiceBeta)
        assertEquals(true, PrefCodec.decode("voiceBeta=TRUE\n").voiceBeta) // case-insensitive true
    }
}
