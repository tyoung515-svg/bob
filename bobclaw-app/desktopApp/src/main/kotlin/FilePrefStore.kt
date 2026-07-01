package com.bobclaw

import com.bobclaw.network.PrefCodec
import com.bobclaw.network.PrefStore
import com.bobclaw.network.UserPrefs
import java.io.File

/**
 * Desktop preferences persistence: stores the user prefs under %APPDATA%/BoBClaw
 * (or ~/.bobclaw) so UI choices (e.g. the UI scale) survive app restarts. Plain
 * `key=value` text via [PrefCodec]; the file is created in the per-user app data dir.
 * Mirrors [FileSessionStore] in structure (lazy file, `runCatching` IO).
 */
class FilePrefStore : PrefStore {
    private val file: File by lazy {
        val base = System.getenv("APPDATA")?.takeIf { it.isNotBlank() }
            ?.let { File(it, "BoBClaw") }
            ?: File(System.getProperty("user.home"), ".bobclaw")
        base.mkdirs()
        File(base, "prefs.txt")
    }

    override fun load(): UserPrefs = runCatching {
        if (file.exists()) PrefCodec.decode(file.readText()) else UserPrefs()
    }.getOrDefault(UserPrefs())

    override fun save(prefs: UserPrefs) {
        runCatching { file.writeText(PrefCodec.encode(prefs)) }
    }
}
