package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun WorkersInFlightTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = stringResource(Res.string.kpi_workers_title),
        value = "5",
        sub = "3 coders, 2 planners",
        modifier = modifier,
    )
    // MOCK: core dispatch state not yet exposed
}
