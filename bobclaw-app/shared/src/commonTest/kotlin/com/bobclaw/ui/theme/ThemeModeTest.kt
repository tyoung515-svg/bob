package com.bobclaw.ui.theme

import kotlin.test.Test
import kotlin.test.assertEquals

/** [resolveThemeMode] — the pure pref+OS → concrete-mode resolver behind the A1 theme toggle. */
class ThemeModeTest {

    @Test
    fun dark_pref_is_always_dark() {
        assertEquals(ThemeMode.DARK, resolveThemeMode("dark", systemInDark = true))
        assertEquals(ThemeMode.DARK, resolveThemeMode("dark", systemInDark = false))
    }

    @Test
    fun light_pref_is_always_light() {
        assertEquals(ThemeMode.LIGHT, resolveThemeMode("light", systemInDark = true))
        assertEquals(ThemeMode.LIGHT, resolveThemeMode("light", systemInDark = false))
    }

    @Test
    fun system_pref_follows_the_os() {
        assertEquals(ThemeMode.DARK, resolveThemeMode("system", systemInDark = true))
        assertEquals(ThemeMode.LIGHT, resolveThemeMode("system", systemInDark = false))
    }

    @Test
    fun unknown_pref_falls_back_to_dark() {
        assertEquals(ThemeMode.DARK, resolveThemeMode("neon", systemInDark = false))
        assertEquals(ThemeMode.DARK, resolveThemeMode("", systemInDark = false))
    }
}
