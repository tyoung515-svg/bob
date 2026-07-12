package com.bobclaw.ui.tiles

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.UpcomingFire
import com.bobclaw.model.formatFireTime
import com.bobclaw.model.upcomingFires
import com.bobclaw.network.RestClient
import com.bobclaw.ui.components.IconGlyph
import com.bobclaw.ui.theme.BoBClawColors
import kotlinx.coroutines.delay
import kotlinx.datetime.Clock
import kotlinx.datetime.TimeZone

/**
 * Home tile (U1/D2): upcoming unattended scheduled runs. Binds to the LIVE `/profiles` route
 * (`RestClient.getProfiles()` → gateway `/profiles` → core `/api/profiles`), reads each
 * profile's `schedule.cron`, and renders "Bob runs <profile> at <next fire>". Real data only —
 * no mock. A profile with a cron but no fireable task is filtered out (mirrors the scheduler).
 */
@Composable
fun ScheduledFiresTile(
    restClient: RestClient?,
    modifier: Modifier = Modifier,
) {
    var fires by remember { mutableStateOf<List<UpcomingFire>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(restClient) {
        while (true) {
            if (restClient == null) {
                loading = false
                error = "Not configured — no gateway URL set"
                delay(30_000)
                continue
            }
            loading = true
            error = null
            try {
                val profiles = restClient.getProfiles()
                fires = upcomingFires(profiles, Clock.System.now(), TimeZone.currentSystemDefault())
                loading = false
            } catch (e: Exception) {
                fires = null
                error = e.message ?: "Unknown error"
                loading = false
            }
            delay(30_000)
        }
    }

    SectionTile(title = stringResource(Res.string.scheduled_fires_title), modifier = modifier) {
        val items = fires
        when {
            loading && items == null -> Text(
                stringResource(Res.string.scheduled_fires_loading),
                color = BoBClawColors.TextSecondary,
                fontSize = 13.sp,
            )
            error != null && items == null -> Text(
                "Failed: $error",
                color = BoBClawColors.TextSecondary,
                fontSize = 12.sp,
            )
            items.isNullOrEmpty() -> Text(
                stringResource(Res.string.scheduled_fires_none),
                color = BoBClawColors.TextSecondary,
                fontSize = 13.sp,
            )
            else -> Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 160.dp)
                    .verticalScroll(rememberScrollState())
            ) {
                items.forEach { fire ->
                    FireRow(fire)
                    Spacer(Modifier.height(6.dp))
                }
            }
        }
    }
}

@Composable
private fun FireRow(fire: UpcomingFire) {
    val whenText = formatFireTime(fire.nextFire, TimeZone.currentSystemDefault())
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        IconGlyph(name = "clock", tint = BoBClawColors.AccentGreen, size = 9.dp)
        Spacer(Modifier.width(6.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = if (whenText != null) {
                    stringResource(Res.string.scheduled_fires_row, fire.profile, whenText)
                } else {
                    stringResource(Res.string.scheduled_fires_row_none, fire.profile)
                },
                color = BoBClawColors.TextPrimary,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                text = fire.task,
                color = BoBClawColors.TextSecondary,
                fontSize = 10.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
