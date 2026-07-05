package com.bobclaw.ui.tiles.kpi

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun TestsPassingTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = stringResource(Res.string.kpi_tests_tests_passing),
        value = "234/234",
        sub = stringResource(Res.string.kpi_tests_all_green),
        modifier = modifier,
    )
    // MOCK: static until wired to CI
}
