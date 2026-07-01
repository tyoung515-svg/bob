package com.bobclaw.ui.tiles

import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.bobclaw.ui.components.Tile
import com.bobclaw.ui.theme.BoBClawColors

@Composable
fun KpiTile(
    label: String,
    value: String,
    sub: String? = null,
    valueColor: Color = BoBClawColors.KpiGreen,
    modifier: Modifier = Modifier,
) {
    Tile(title = label, modifier = modifier) {
        Text(
            text = value,
            color = valueColor,
            fontSize = 24.sp,
            fontWeight = FontWeight.Bold,
        )
        if (sub != null) {
            Text(
                text = sub,
                color = BoBClawColors.TextSecondary,
                fontSize = 11.sp,
            )
        }
    }
}
