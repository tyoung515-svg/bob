package com.bobclaw.auth

import com.bobclaw.model.TokenPair
import com.bobclaw.network.NoopSessionStore
import com.bobclaw.network.RestClient
import com.bobclaw.network.SessionStore
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.datetime.Clock
import kotlin.time.Duration.Companion.minutes

class AuthManager(
    private val restClient: RestClient,
    private val sessionStore: SessionStore = NoopSessionStore,
) {
    private var tokenPair: TokenPair? = null
    private var accessTokenExpiryEpochMillis: Long = 0L
    private val refreshMutex = Mutex()

    val isLoggedIn: Boolean
        get() = tokenPair != null

    suspend fun login(password: String, totp: String? = null): TokenPair {
        val tokens = restClient.login(password = password, totpCode = totp)
        saveTokens(tokens)
        return tokens
    }

    /**
     * Attempt to restore a persisted session on launch. Uses the stored refresh token to mint a
     * fresh access token (and a rotated refresh token, which is re-persisted). Returns false (and
     * clears the bad token) if there's nothing stored or the refresh is expired/invalid.
     */
    suspend fun tryRestore(): Boolean {
        val refresh = sessionStore.loadRefreshToken() ?: return false
        return try {
            saveTokens(restClient.refreshToken(refresh))
            true
        } catch (e: Throwable) {
            sessionStore.saveRefreshToken(null)
            false
        }
    }

    fun logout() {
        tokenPair = null
        accessTokenExpiryEpochMillis = 0L
        restClient.updateTokens(null)
        sessionStore.saveRefreshToken(null)
    }

    suspend fun getAccessToken(): String? {
        val current = tokenPair ?: return null
        if (!isAccessTokenStale()) {
            return current.access
        }

        val refreshed = refreshMutex.withLock {
            val latest = tokenPair ?: return null
            if (!isAccessTokenStale()) {
                latest
            } else {
                val newTokens = restClient.refreshToken(latest.refresh)
                saveTokens(newTokens)
                newTokens
            }
        }

        return refreshed?.access
    }

    private fun saveTokens(tokens: TokenPair) {
        tokenPair = tokens
        restClient.updateTokens(tokens)
        sessionStore.saveRefreshToken(tokens.refresh)

        val nowEpochMillis = Clock.System.now().toEpochMilliseconds()
        accessTokenExpiryEpochMillis = when {
            tokens.expiresAtEpochSeconds != null -> tokens.expiresAtEpochSeconds * 1_000
            tokens.expiresInSeconds != null -> nowEpochMillis + (tokens.expiresInSeconds * 1_000)
            else -> nowEpochMillis + 55.minutes.inWholeMilliseconds
        }
    }

    private fun isAccessTokenStale(): Boolean {
        val now = Clock.System.now().toEpochMilliseconds()
        val refreshSkewMillis = 60_000L
        return now >= (accessTokenExpiryEpochMillis - refreshSkewMillis)
    }
}
