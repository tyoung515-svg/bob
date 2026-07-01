package com.bobclaw.auth

actual class BiometricAuth {
    actual val isAvailable: Boolean = true
    actual suspend fun authenticate(reason: String): Boolean = true
}
