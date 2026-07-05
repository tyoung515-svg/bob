package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun BuildSessionsTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = stringResource(Res.string.kpi_build_sessions_label),
        value = "8",
        sub = "2 running",
        modifier = modifier,
    )
    // MOCK: static value; replace with RestClient.getBuilds() count
}
