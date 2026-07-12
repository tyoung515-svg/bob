package com.bobclaw

import me.friwi.jcefmaven.CefAppBuilder
import org.cef.CefApp
import org.cef.CefClient
import org.cef.browser.CefBrowser
import org.cef.browser.CefFrame
import org.cef.browser.CefMessageRouter
import org.cef.callback.CefQueryCallback
import org.cef.handler.CefLoadHandlerAdapter
import org.cef.handler.CefMessageRouterHandlerAdapter
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * App-lifetime JCEF (embedded Chromium) owner. init() downloads + initializes Chromium on a
 * daemon thread (first run ~150MB into ~/.bobclaw/jcef-bundle, cached after). `ready` flips true
 * when the CefApp + client are up; the UI polls it. newBrowser() makes a windowed CefBrowser whose
 * uiComponent embeds in a Compose SwingPanel. dispose() on app close.
 */
object JcefManager {
    @Volatile
    var ready: Boolean = false
        private set

    private var app: CefApp? = null
    private var client: CefClient? = null
    private val starting = AtomicBoolean(false)

    fun init() {
        if (!starting.compareAndSet(false, true)) return
        thread(name = "jcef-init", isDaemon = true) {
            try {
                println("[jcef] init starting (first run downloads ~150MB)...")
                val builder = CefAppBuilder()
                builder.setInstallDir(File(System.getProperty("user.home"), ".bobclaw/jcef-bundle"))
                builder.setProgressHandler { state, pct ->
                    println("[jcef] $state${if (pct >= 0f) " ${pct.toInt()}%" else ""}")
                }
                builder.cefSettings.windowless_rendering_enabled = false // windowed: embed via SwingPanel
                val a = builder.build()
                app = a
                client = a.createClient()
                ready = true
                println("[jcef] READY")
            } catch (e: Throwable) {
                println("[jcef] INIT ERROR: ${e.message}")
                e.printStackTrace()
            }
        }
    }

    /** Create a windowed browser (osr=false, transparent=false). Null if not ready yet. */
    fun newBrowser(initialUrl: String = "about:blank"): CefBrowser? =
        client?.createBrowser(initialUrl, false, false)

    // ── Memory-graph bridge (U4b): JS ⇄ Kotlin for the 3D canvas ─────────────────

    /**
     * Per-graph-browser bridge. [onQuery] receives the JSON a `window.cefQuery` call sent
     * (JS → Kotlin node-select); [onLoadEnd] fires when the page finishes loading (so the
     * caller can push the initial graph once `window.BClaw` is defined).
     */
    interface GraphBridge {
        fun onQuery(request: String)
        fun onLoadEnd()
    }

    // Shared client is used by every browser, so we route by the CefBrowser instance. The
    // artifact pane never calls cefQuery, so only graph browsers ever match here.
    private val bridges = ConcurrentHashMap<CefBrowser, GraphBridge>()
    private val routerInstalled = AtomicBoolean(false)

    private fun ensureGraphRouterInstalled(c: CefClient) {
        if (!routerInstalled.compareAndSet(false, true)) return
        // Message routers must be added to the client BEFORE the browser is created.
        val router = CefMessageRouter.create()
        router.addHandler(object : CefMessageRouterHandlerAdapter() {
            override fun onQuery(
                browser: CefBrowser?,
                frame: CefFrame?,
                queryId: Long,
                request: String?,
                persistent: Boolean,
                callback: CefQueryCallback?,
            ): Boolean {
                val bridge = browser?.let { bridges[it] } ?: return false
                runCatching { bridge.onQuery(request ?: "") }
                callback?.success("")
                return true
            }
        }, true)
        c.addMessageRouter(router)
        c.addLoadHandler(object : CefLoadHandlerAdapter() {
            override fun onLoadEnd(browser: CefBrowser?, frame: CefFrame?, httpStatusCode: Int) {
                if (frame != null && !frame.isMain) return
                browser?.let { bridges[it] }?.let { runCatching { it.onLoadEnd() } }
            }
        })
    }

    /** Windowed browser wired to [bridge] and loading [url] (a bundled file:// graph page). */
    fun newGraphBrowser(url: String, bridge: GraphBridge): CefBrowser? {
        val c = client ?: return null
        ensureGraphRouterInstalled(c)
        val browser = c.createBrowser(url, false, false)
        bridges[browser] = bridge
        return browser
    }

    fun releaseGraphBrowser(browser: CefBrowser) {
        bridges.remove(browser)
        runCatching { browser.close(true) }
    }

    fun dispose() {
        runCatching { app?.dispose() }
    }
}
