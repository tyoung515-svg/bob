package com.bobclaw.ui.tiles

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.bobclaw.ui.components.Tile

@Composable
fun SectionTile(
    title: String,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    Tile(title = title, modifier = modifier, content = content)
}
