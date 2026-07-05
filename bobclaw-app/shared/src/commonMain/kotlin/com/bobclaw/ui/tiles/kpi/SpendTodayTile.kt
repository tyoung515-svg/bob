package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun SpendTodayTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = stringResource(Res.string.kpi_spend_today_label),
        value = "$12.47",
        sub = stringResource(Res.string.kpi_spend_sub),
        modifier = modifier,
    )
    // MOCK: core cost tracker not yet exposed
}
