package com.bobclaw.auth

expect class BiometricAuth() {
    val isAvailable: Boolean
    suspend fun authenticate(reason: String): Boolean
}
