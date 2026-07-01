package com.bobclaw.auth

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

actual class BiometricAuth {
    actual val isAvailable: Boolean = true

    actual suspend fun authenticate(reason: String): Boolean {
        return withContext(Dispatchers.IO) {
            val S = "${'$'}"
            val script = ("[Windows.Security.Credentials.UI.UserConsentVerifier, " +
                "Windows.Security.Credentials.UI, ContentType=WindowsRuntime] | Out-Null; " +
                "${S}op = [Windows.Security.Credentials.UI.UserConsentVerifier]" +
                "::RequestVerificationAsync('$reason'); " +
                "while (-not ${S}op.IsCompleted) { Start-Sleep -Milliseconds 100 }; " +
                "${S}op.GetAwaiter().GetResult()")

            val pb = ProcessBuilder("powershell", "-NoProfile", "-Command", script)
            pb.redirectErrorStream(true)
            val process = pb.start()
            val output = process.inputStream.bufferedReader().readText().trim()
            val exitCode = process.waitFor()

            if (exitCode != 0) {
                throw BiometricException("PowerShell process failed (exit=$exitCode): $output")
            }

            when (output) {
                "Verified" -> true
                else -> throw BiometricException("Windows Hello declined: $output")
            }
        }
    }
}

class BiometricException(message: String) : Exception(message)
