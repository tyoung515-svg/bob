package com.bobclaw

import com.bobclaw.network.SessionStore
import java.io.File

/**
 * Desktop session persistence: stores the refresh token under %APPDATA%/BoBClaw
 * (or ~/.bobclaw) so login survives app restarts. Plain text — a single rotating
 * refresh token; the file is created in the per-user app data dir.
 */
class FileSessionStore : SessionStore {
    private val file: File by lazy {
        val base = System.getenv("APPDATA")?.takeIf { it.isNotBlank() }
            ?.let { File(it, "BoBClaw") }
            ?: File(System.getProperty("user.home"), ".bobclaw")
        base.mkdirs()
        File(base, "session.txt")
    }

    override fun loadRefreshToken(): String? = runCatching {
        if (file.exists()) file.readText().trim().ifBlank { null } else null
    }.getOrNull()

    override fun saveRefreshToken(token: String?) {
        runCatching {
            if (token.isNullOrBlank()) {
                if (file.exists()) file.delete()
            } else {
                file.writeText(token)
            }
        }
    }
}
