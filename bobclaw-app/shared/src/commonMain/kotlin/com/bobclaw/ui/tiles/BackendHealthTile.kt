package com.bobclaw.ui.tiles

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.HealthStatus
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.BoBClawColors
import kotlinx.coroutines.delay

private val HealthRed = Color(0xFFE74C3C)
private val HealthYellow = Color(0xFFF1C40F)

@Composable
fun BackendHealthTile(
    restClient: RestClient?,
    modifier: Modifier = Modifier,
) {
    var healthList by remember { mutableStateOf<List<HealthStatus>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(restClient) {
        while (true) {
            if (restClient == null) {
                loading = false
                error = "Not configured — no gateway URL set"
                delay(10_000)
                continue
            }
            loading = true
            error = null
            try {
                healthList = restClient.getHealth()
                loading = false
            } catch (e: Exception) {
                healthList = null
                error = e.message ?: "Unknown error"
                loading = false
            }
            delay(10_000)
        }
    }

    SectionTile(title = stringResource(Res.string.backend_health_title), modifier = modifier) {
        if (loading && healthList == null) {
            Text(
                stringResource(Res.string.backend_health_checking_backends),
                color = BoBClawColors.TextSecondary,
                fontSize = 13.sp,
            )
        } else if (error != null && healthList == null) {
            Text(
                "Failed: ${error}",
                color = HealthRed,
                fontSize = 12.sp,
            )
        } else {
            val items = healthList
            if (items.isNullOrEmpty()) {
                Text(
                    stringResource(Res.string.backend_health_no_backends_registered),
                    color = BoBClawColors.TextSecondary,
                    fontSize = 13.sp,
                )
            } else {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(max = 160.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    items.forEach { backend ->
                        HealthRow(backend)
                        Spacer(Modifier.height(6.dp))
                    }
                }
            }
        }
    }
}

@Composable
private fun HealthRow(health: HealthStatus) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        val dotColor = when (health.status.lowercase()) {
            "ok", "healthy", "up" -> BoBClawColors.KpiGreen
            "degraded", "warn" -> HealthYellow
            else -> HealthRed
        }
        Text(
            text = "●",
            color = dotColor,
            fontSize = 10.sp,
        )
        Spacer(Modifier.width(6.dp))
        Text(
            text = health.name,
            color = BoBClawColors.TextPrimary,
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
            modifier = Modifier.weight(1f),
        )
        if (health.latencyMs != null) {
            Text(
                text = "${health.latencyMs}ms",
                color = BoBClawColors.TextSecondary,
                fontSize = 10.sp,
            )
        }
    }
    if (health.message != null) {
        Text(
            text = health.message,
            color = BoBClawColors.TextSecondary,
            fontSize = 10.sp,
            modifier = Modifier.padding(start = 16.dp),
        )
    }
}


