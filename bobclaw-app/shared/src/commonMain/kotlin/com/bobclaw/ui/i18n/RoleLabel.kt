package com.bobclaw.ui.i18n

import androidx.compose.runtime.Composable
import org.jetbrains.compose.resources.stringResource
import com.bobclaw.shared.resources.*

/**
 * UI-ONLY display label for a backend role value (apex/worker/critic). The backend value itself is
 * NEVER changed — this maps it to a localized label purely at render time. Unknown roles pass
 * through unchanged (so new backend roles never break the UI).
 */
@Composable
fun roleLabel(role: String): String = when (role) {
    "apex" -> stringResource(Res.string.role_apex)
    "worker" -> stringResource(Res.string.role_worker)
    "critic" -> stringResource(Res.string.role_critic)
    else -> role
}
