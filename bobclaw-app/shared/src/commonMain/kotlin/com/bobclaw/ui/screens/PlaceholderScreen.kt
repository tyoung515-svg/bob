package com.bobclaw.ui.screens

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors

/**
 * Generic "coming soon" surface (DESIGN §4 / §10 phasing). Used by the rail for the
 * destinations whose real screens land in later lanes (Council / Teams / Routing /
 * Approvals) plus the Settings action (lane 4). Centered title + caption, theme tokens only.
 */
@Composable
fun PlaceholderScreen(name: String, modifier: Modifier = Modifier) {
    val colors = LocalBoBClawColors
    GradientBackground(modifier = modifier) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    text = name,
                    style = BoBClawType.title,
                    color = colors.textPrimary,
                    textAlign = TextAlign.Center,
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    text = "coming soon",
                    style = BoBClawType.monoCaption,
                    color = colors.textMuted,
                    textAlign = TextAlign.Center,
                )
            }
        }
    }
}
