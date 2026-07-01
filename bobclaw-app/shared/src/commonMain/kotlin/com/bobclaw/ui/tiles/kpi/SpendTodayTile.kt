package com.bobclaw.ui.tiles.kpi

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun SpendTodayTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = "$ Today",
        value = "$12.47",
        sub = "est. daily",
        modifier = modifier,
    )
    // MOCK: core cost tracker not yet exposed
}
