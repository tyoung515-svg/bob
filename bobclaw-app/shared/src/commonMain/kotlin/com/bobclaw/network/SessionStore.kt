package com.bobclaw.network

/**
 * Persists the refresh token across app launches so the user isn't re-prompted (and re-TOTP'd)
 * every start. Platform-specific implementations do the actual IO; commonMain only sees this.
 */
interface SessionStore {
    fun loadRefreshToken(): String?
    /** Pass null to clear the stored session (logout / invalid refresh). */
    fun saveRefreshToken(token: String?)
}

/** Default no-op store (no persistence) — used by callers/platforms that don't wire one. */
object NoopSessionStore : SessionStore {
    override fun loadRefreshToken(): String? = null
    override fun saveRefreshToken(token: String?) {}
}
