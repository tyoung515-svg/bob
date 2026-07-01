package com.bobclaw

import me.friwi.jcefmaven.CefAppBuilder
import org.cef.CefApp
import org.cef.CefClient
import org.cef.browser.CefBrowser
import java.io.File
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

    fun dispose() {
        runCatching { app?.dispose() }
    }
}
