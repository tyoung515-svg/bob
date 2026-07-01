package com.bobclaw.auth

actual class BiometricAuth {
    actual val isAvailable: Boolean = false
    actual suspend fun authenticate(reason: String): Boolean = false
}
