package com.bobclaw

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.awt.SwingPanel
import androidx.compose.ui.platform.LocalDensity
import com.bobclaw.ui.LocalInteropDensity
import kotlinx.coroutines.delay
import java.awt.BorderLayout
import java.util.Base64
import javax.swing.JPanel

/**
 * Desktop artifact renderer: embeds a JCEF browser (Chromium) in a Compose SwingPanel.
 * Renders either a [url] (e.g. file:// to a scratch artifact) or inline [html] (base64 data URI).
 *
 * The browser is created WITH the target URL (keyed on it) rather than created-then-loadURL'd —
 * loading too early on a not-yet-realized browser leaves the surface blank/black. Injected into
 * ChatScreen as the `artifactRenderer` lambda so commonMain never imports JCEF.
 */
@Composable
fun WebArtifactView(html: String?, url: String?, modifier: Modifier = Modifier) {
    var ready by remember { mutableStateOf(JcefManager.ready) }
    LaunchedEffect(Unit) {
        while (!JcefManager.ready) delay(300)
        ready = true
    }

    if (!ready) {
        Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Text("Starting browser…") }
        return
    }

    val target = when {
        !url.isNullOrBlank() -> url
        !html.isNullOrBlank() ->
            "data:text/html;base64," + Base64.getEncoder().encodeToString(html.toByteArray(Charsets.UTF_8))
        else -> "about:blank"
    }

    // Recreate the browser with the target URL whenever it changes — avoids the
    // create-then-loadURL timing race that leaves the surface blank.
    val browser = remember(target) { JcefManager.newBrowser(target) }
    if (browser == null) {
        Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Text("Browser unavailable") }
        return
    }

    // Re-provide the system density + BorderLayout wrapper so the JCEF surface fills its slot
    // under the app's uiScale density override (see MemoryGraphView / LocalInteropDensity).
    val interopDensity = LocalInteropDensity.current ?: LocalDensity.current
    CompositionLocalProvider(LocalDensity provides interopDensity) {
        SwingPanel(
            modifier = modifier.fillMaxSize(),
            factory = { JPanel(BorderLayout()).apply { add(browser.uiComponent, BorderLayout.CENTER) } },
            update = { it.revalidate() },
        )
    }

    DisposableEffect(browser) {
        onDispose { runCatching { browser.close(true) } }
    }
}
