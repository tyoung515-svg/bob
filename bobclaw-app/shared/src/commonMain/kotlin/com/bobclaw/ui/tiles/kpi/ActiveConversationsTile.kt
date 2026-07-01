package com.bobclaw.ui.tiles.kpi

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun ActiveConversationsTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = "Active Conversations",
        value = "12",
        sub = "3 active now",
        modifier = modifier,
    )
    // MOCK: static value; replace with RestClient.getConversations() count
}
