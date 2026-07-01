package com.bobclaw.ui.tiles.kpi

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.tiles.KpiTile

@Composable
fun TestsPassingTile(modifier: Modifier = Modifier) {
    KpiTile(
        label = "Tests Passing",
        value = "234/234",
        sub = "all green",
        modifier = modifier,
    )
    // MOCK: static until wired to CI
}
