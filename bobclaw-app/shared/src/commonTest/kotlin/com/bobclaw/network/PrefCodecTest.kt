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
}
