package com.bobclaw

import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import com.bobclaw.ui.App

fun main() = application {
    // Kick off embedded-Chromium init (downloads ~150MB on first run, cached after). The canvas
    // pane polls JcefManager.ready; the rest of the app works regardless.
    JcefManager.init()
    Window(
        onCloseRequest = { JcefManager.dispose(); exitApplication() },
        title = "BoBClaw",
    ) {
        // Tint the native Win11 caption to the command-center dark theme once the window has a
        // native peer (fail-safe no-op off-Windows / pre-Win11). `window` is the FrameWindowScope's.
        LaunchedEffect(Unit) { WindowTheme.applyDark(window) }
        App(
            sessionStore = FileSessionStore(),
            prefStore = FilePrefStore(),
            // Inject the JCEF-backed artifact renderer so commonMain stays platform-agnostic.
            artifactRenderer = { html, url, modifier -> WebArtifactView(html, url, modifier) },
            // Inject the JCEF-backed Memory 3D graph renderer (U4b) — bundled three.js +
            // 3d-force-graph, no CDN; JS↔Kotlin bridge for select/fly-to/forget.
            memoryGraphRenderer = { json, oneShot, onSelect, modifier ->
                MemoryGraphView(json, oneShot, onSelect, modifier)
            },
            applyPlatformLocale = { tag -> java.util.Locale.setDefault(when (tag) { "zh-Hans" -> java.util.Locale.forLanguageTag("zh-CN"); "zh-Hant" -> java.util.Locale.forLanguageTag("zh-TW"); else -> java.util.Locale.ENGLISH }) },
        )
    }
}
