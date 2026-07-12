package com.bobclaw

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.awt.SwingPanel
import androidx.compose.ui.platform.LocalDensity
import com.bobclaw.ui.LocalInteropDensity
import com.bobclaw.ui.screens.GraphOneShot
import kotlinx.coroutines.delay
import org.cef.browser.CefBrowser
import java.awt.BorderLayout
import java.io.File
import java.util.Base64
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import javax.swing.JPanel
import javax.swing.SwingUtilities

/**
 * Extracts the bundled Memory-graph web assets (three.js + 3d-force-graph + graph.html, all
 * MIT, all LOCAL — no CDN) from the app resources to ``~/.bobclaw/memory-graph/`` so JCEF can
 * load them over ``file://`` with the network disabled. Overwrites each launch so a rebuilt jar
 * ships fresh assets; the resolved page URL is cached for the process.
 */
object MemoryGraphAssets {
    private val ASSETS = listOf(
        "graph.html",
        "three.min.js",
        "3d-force-graph.min.js",
        "LICENSE-three.txt",
        "LICENSE-3d-force-graph.txt",
        "NOTICE.txt",
    )

    @Volatile
    private var pageUrl: String? = null

    @Synchronized
    fun ensureExtracted(): String? {
        pageUrl?.let { return it }
        return try {
            val dir = File(System.getProperty("user.home"), ".bobclaw/memory-graph")
            dir.mkdirs()
            for (name in ASSETS) {
                val stream = javaClass.getResourceAsStream("/memory_graph/$name")
                if (stream == null) {
                    if (name == "graph.html") return null  // the page itself is mandatory
                    continue
                }
                stream.use { input -> File(dir, name).outputStream().use { input.copyTo(it) } }
            }
            val html = File(dir, "graph.html")
            if (!html.exists()) return null
            // file:/// + forward-slashed absolute path (Chromium-friendly); encode spaces.
            ("file:///" + html.absolutePath.replace('\\', '/').replace(" ", "%20")).also { pageUrl = it }
        } catch (e: Throwable) {
            println("[memory-graph] asset extraction failed: ${e.message}")
            null
        }
    }
}

private fun b64(s: String): String = Base64.getEncoder().encodeToString(s.toByteArray(Charsets.UTF_8))

// Minimal, dependency-free extraction of the selected node id from the cefQuery JSON
// (`{"type":"select","id":"..."}`). Node ids never contain a double-quote, so this is exact.
private val SELECT_ID = Regex("\"id\"\\s*:\\s*\"(.*?)\"")
private fun parseSelectId(request: String): String? =
    if (request.contains("\"select\"")) SELECT_ID.find(request)?.groupValues?.getOrNull(1)?.takeIf { it.isNotEmpty() } else null

/**
 * Desktop Memory-graph renderer (U4b): embeds the JCEF browser showing the bundled 3D
 * force-graph and bridges it to Compose. Matches the `MemoryGraphRenderer` seam so it can be
 * injected from [Main] exactly like `WebArtifactView`.
 *
 *  - [graphJson]      pushed to `BClaw.render` (base64/UTF-8 safe) on change + on page load.
 *  - [oneShot]        fly-to / remove, applied once per identity (search + forget).
 *  - [onNodeSelected] JS → Kotlin node click (marshalled to the UI thread).
 */
@Composable
fun MemoryGraphView(
    graphJson: String?,
    oneShot: GraphOneShot?,
    onNodeSelected: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var ready by remember { mutableStateOf(JcefManager.ready) }
    LaunchedEffect(Unit) {
        while (!JcefManager.ready) delay(300)
        ready = true
    }
    if (!ready) {
        Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Text("Starting 3D graph…") }
        return
    }

    val pageUrl = remember { MemoryGraphAssets.ensureExtracted() }
    if (pageUrl == null) {
        Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Text("Graph assets unavailable") }
        return
    }

    val loaded = remember { AtomicBoolean(false) }
    val latestJson = remember { AtomicReference<String?>(null) }
    val browserRef = remember { AtomicReference<CefBrowser?>(null) }
    val onSelectRef = remember { AtomicReference(onNodeSelected) }
    SideEffect { onSelectRef.set(onNodeSelected) }

    fun runJs(js: String) {
        browserRef.get()?.executeJavaScript(js, pageUrl, 0)
    }
    fun pushRender(json: String?) {
        if (json != null) runJs("if(window.BClaw){BClaw.render(BClaw.parse('${b64(json)}'));}")
    }

    val browser = remember {
        val bridge = object : JcefManager.GraphBridge {
            override fun onQuery(request: String) {
                val id = parseSelectId(request) ?: return
                SwingUtilities.invokeLater { onSelectRef.get().invoke(id) }
            }
            override fun onLoadEnd() {
                loaded.set(true)
                pushRender(latestJson.get())  // (re)push once BClaw is defined
            }
        }
        JcefManager.newGraphBrowser(pageUrl, bridge).also { browserRef.set(it) }
    }
    if (browser == null) {
        Box(modifier.fillMaxSize(), contentAlignment = Alignment.Center) { Text("Browser unavailable") }
        return
    }

    LaunchedEffect(graphJson) {
        latestJson.set(graphJson)
        if (loaded.get()) pushRender(graphJson)
    }
    LaunchedEffect(oneShot) {
        val cmd = oneShot ?: return@LaunchedEffect
        if (!loaded.get()) return@LaunchedEffect
        when (cmd.kind) {
            GraphOneShot.Kind.FLY_TO -> runJs("if(window.BClaw){BClaw.flyTo(BClaw.parseStr('${b64(cmd.nodeId)}'));}")
            GraphOneShot.Kind.REMOVE -> runJs("if(window.BClaw){BClaw.remove(BClaw.parseStr('${b64(cmd.nodeId)}'));}")
        }
    }

    // Re-provide the un-scaled system density so SwingPanel sizes the heavyweight JCEF surface to
    // fill its Compose slot. Under the app's uiScale LocalDensity override, SwingPanel otherwise
    // maps the slot's px through the scaled density but paints at system scale, shrinking the
    // browser to 1/uiScale of its box (the black gutters). BorderLayout keeps AWT relaying the
    // native child on resize. (U4b fix.)
    val interopDensity = LocalInteropDensity.current ?: LocalDensity.current
    CompositionLocalProvider(LocalDensity provides interopDensity) {
        SwingPanel(
            modifier = modifier.fillMaxSize(),
            factory = { JPanel(BorderLayout()).apply { add(browser.uiComponent, BorderLayout.CENTER) } },
            update = { it.revalidate() },
        )
    }
    DisposableEffect(browser) {
        onDispose { JcefManager.releaseGraphBrowser(browser) }
    }
}
