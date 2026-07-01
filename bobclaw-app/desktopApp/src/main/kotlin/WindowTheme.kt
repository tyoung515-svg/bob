package com.bobclaw

import com.sun.jna.Memory
import com.sun.jna.Native
import com.sun.jna.Pointer
import com.sun.jna.platform.win32.WinDef
import com.sun.jna.win32.StdCallLibrary
import com.sun.jna.win32.W32APIOptions
import java.awt.Window

/**
 * Tints the native Windows 11 title-bar (caption) to the command-center dark theme so the OS
 * caption reads as one continuous surface with the app, instead of the default lighter grey
 * (GUI lane 4b follow-up — the operator chose "match app dark").
 *
 * Uses the DWM `DWMWA_CAPTION_COLOR` / `DWMWA_TEXT_COLOR` window attributes (Windows 11 build
 * 22000+). Fully **fail-safe**: any failure — non-Windows OS, pre-Win11, dwmapi/JNA unavailable,
 * no native peer yet — is swallowed and the OS keeps drawing its default caption. JNA resolves
 * via `jna-platform` (already transitively present through jcefmaven). Desktop-only; `commonMain`
 * never sees this.
 */
object WindowTheme {
    private interface Dwmapi : StdCallLibrary {
        fun DwmSetWindowAttribute(hwnd: WinDef.HWND, dwAttribute: Int, pvAttribute: Pointer, cbAttribute: Int): Int
    }

    private const val DWMWA_CAPTION_COLOR = 35  // Win11 22000+
    private const val DWMWA_TEXT_COLOR = 36

    private val dwm: Dwmapi? by lazy {
        runCatching {
            if (!System.getProperty("os.name").orEmpty().startsWith("Windows", ignoreCase = true)) return@runCatching null
            Native.load("dwmapi", Dwmapi::class.java, W32APIOptions.DEFAULT_OPTIONS)
        }.getOrNull()
    }

    /** Windows COLORREF packs bytes as 0x00BBGGRR — NOT RGB. */
    private fun colorref(r: Int, g: Int, b: Int): Int = (b shl 16) or (g shl 8) or r

    /** Tint [window]'s caption to the dark `rail` (#0B0E10) with light `textPrimary` (#E6EDF1) title text. */
    fun applyDark(window: Window) {
        runCatching {
            val d = dwm ?: return
            val ptr: Pointer = Native.getWindowPointer(window) ?: return
            val hwnd = WinDef.HWND(ptr)
            fun set(attr: Int, cr: Int) =
                d.DwmSetWindowAttribute(hwnd, attr, Memory(4).apply { setInt(0L, cr) }, 4)
            set(DWMWA_CAPTION_COLOR, colorref(0x0B, 0x0E, 0x10)) // rail
            set(DWMWA_TEXT_COLOR, colorref(0xE6, 0xED, 0xF1))    // textPrimary
        }
    }
}
