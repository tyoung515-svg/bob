package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import com.bobclaw.network.RestClient
import com.bobclaw.ui.tiles.KpiTile
import kotlinx.coroutines.delay

/**
 * Live conversation count (U1/D2 real-data-or-delete): binds to GET /conversations
 * (`RestClient.getConversations`) — no longer a static mock. Value = number of active
 * (non-archived) conversations the gateway returns; refreshed on the same 10s cadence as the
 * other Home tiles. Fails soft to "—" so a flaky gateway never shows a fake number.
 */
@Composable
fun ActiveConversationsTile(
    restClient: RestClient? = null,
    modifier: Modifier = Modifier,
) {
    var count by remember { mutableStateOf<Int?>(null) }
    var errored by remember { mutableStateOf(false) }

    LaunchedEffect(restClient) {
        if (restClient == null) {
            errored = true
            return@LaunchedEffect
        }
        while (true) {
            try {
                count = restClient.getConversations(limit = 100, offset = 0).size
                errored = false
            } catch (_: Exception) {
                errored = true
            }
            delay(10_000)
        }
    }

    KpiTile(
        label = stringResource(Res.string.kpi_active_conv_active_conversations),
        value = count?.toString() ?: if (errored) "—" else "…",
        sub = stringResource(Res.string.kpi_active_conv_sub),
        modifier = modifier,
    )
}
