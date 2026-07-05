package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun ActiveConversationsTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = stringResource(Res.string.kpi_active_conv_active_conversations),
        value = "12",
        sub = "3 active now",
        modifier = modifier,
    )
    // MOCK: static value; replace with RestClient.getConversations() count
}
