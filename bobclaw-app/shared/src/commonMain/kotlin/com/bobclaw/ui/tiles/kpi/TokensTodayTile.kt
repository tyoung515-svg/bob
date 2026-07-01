package com.bobclaw.ui.tiles.kpi

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun TokensTodayTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = "Tokens Today",
        value = "147.2K",
        sub = "≈$2.94",
        modifier = modifier,
    )
    // MOCK: core cost tracker not yet exposed
}
