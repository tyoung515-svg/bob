package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun TokensTodayTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = stringResource(Res.string.kpi_tokens_today_label),
        value = "147.2K",
        sub = "≈$2.94",
        modifier = modifier,
    )
    // MOCK: core cost tracker not yet exposed
}
