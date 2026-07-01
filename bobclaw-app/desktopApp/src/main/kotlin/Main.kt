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
        )
    }
}
