package com.bobclaw.ui.screens

import com.bobclaw.ui.i18n.roleLabel

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.bobclaw.model.RoutingFace
import com.bobclaw.model.RoutingView
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors

private val ErrorRed = Color(0xFFE74C3C)

/**
 * JOAT v0 routing-view — a read-only home for the role/team router (DESIGN §4
 * rail destination "Routing"). Fetches the gateway `GET /routing-view` proxy and
 * renders the live faces → role → resolved-backend map. The team chips preview any
 * built-in fleet via `?team=` without changing the process default. Honest about
 * v0: when [RoutingView.liveProbe] is false the RESOLVED column is the DECLARED
 * mapping, not health-checked.
 */
@Composable
fun RoutingScreen(
    restClient: RestClient?,
    modifier: Modifier = Modifier,
) {
    var selectedTeam by remember { mutableStateOf<String?>(null) }
    var view by remember { mutableStateOf<RoutingView?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(restClient, selectedTeam) {
        if (restClient == null) {
            loading = false
            error = "Not configured — no gateway URL set"
            return@LaunchedEffect
        }
        loading = true
        error = null
        try {
            view = restClient.getRoutingView(selectedTeam)
            loading = false
        } catch (e: Exception) {
            view = null
            error = e.message ?: "Unknown error"
            loading = false
        }
    }

    val colors = LocalBoBClawColors
    GradientBackground(modifier = modifier) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(20.dp)
                .verticalScroll(rememberScrollState())
        ) {
            Text(stringResource(Res.string.routing_title), style = BoBClawType.title, color = colors.textPrimary)
            Spacer(Modifier.height(4.dp))
            val current = view
            Text(
                text = stringResource(Res.string.routing_active_team, current?.activeTeam ?: stringResource(Res.string.routing_default_team_fallback)),
                style = BoBClawType.monoCaption,
                color = colors.textSecondary,
            )

            // Honesty badge: the v0 probe is a no-op → RESOLVED is the declared mapping.
            if (current != null && !current.liveProbe) {
                Spacer(Modifier.height(8.dp))
                Box(
                    modifier = Modifier
                        .clip(BoBClawShapes.cell)
                        .background(colors.surfaceAccent, BoBClawShapes.cell)
                        .padding(horizontal = 10.dp, vertical = 4.dp)
                ) {
                    Text(
                        text = stringResource(Res.string.routing_declared_mapping),
                        style = BoBClawType.monoCaption,
                        color = colors.textSecondary,
                    )
                }
            }

            // Team preview chips: "default" (per-face passthrough) + every built-in team.
            if (current != null) {
                Spacer(Modifier.height(12.dp))
                Row(
                    modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    TeamChip(stringResource(Res.string.routing_default_team), selectedTeam == null) { selectedTeam = null }
                    current.teams.forEach { team ->
                        TeamChip(team, selectedTeam == team) { selectedTeam = team }
                    }
                }
            }

            Spacer(Modifier.height(16.dp))

            when {
                loading && current == null ->
                    Text(stringResource(Res.string.routing_loading), style = BoBClawType.body, color = colors.textSecondary)
                error != null && current == null ->
                    Text("Failed: $error", style = BoBClawType.body, color = ErrorRed)
                current == null || current.faces.isEmpty() ->
                    Text(stringResource(Res.string.routing_no_faces), style = BoBClawType.body, color = colors.textSecondary)
                else -> RoutingTable(current.faces)
            }
        }
    }
}

@Composable
private fun TeamChip(label: String, active: Boolean, onClick: () -> Unit) {
    val colors = LocalBoBClawColors
    Box(
        modifier = Modifier
            .clip(BoBClawShapes.full)
            .background(if (active) colors.accent else colors.surfaceAccent, BoBClawShapes.full)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 5.dp),
    ) {
        Text(
            text = label,
            style = BoBClawType.monoCaption,
            color = if (active) colors.onAccent else colors.textSecondary,
        )
    }
}

@Composable
private fun RoutingTable(faces: List<RoutingFace>) {
    val colors = LocalBoBClawColors
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
            HeaderCell(stringResource(Res.string.routing_header_face), 2f)
            HeaderCell(stringResource(Res.string.routing_header_role), 1f)
            HeaderCell(stringResource(Res.string.routing_header_resolved), 2f)
            HeaderCell(stringResource(Res.string.routing_header_escalation), 3f)
            HeaderCell(stringResource(Res.string.routing_header_tools), 1f)
        }
        Spacer(Modifier.fillMaxWidth().height(1.dp).background(colors.borderSection))
        faces.forEach { face ->
            Row(
                modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                BodyCell(face.id, 2f, colors.textPrimary, FontWeight.Medium)
                BodyCell(face.role?.let { roleLabel(it) } ?: "—", 1f, colors.textSecondary)
                BodyCell(face.resolvedBackend.ifBlank { "—" }, 2f, colors.accent, FontWeight.Medium)
                BodyCell(
                    if (face.escalationChain.isEmpty()) "—" else face.escalationChain.joinToString(" → "),
                    3f,
                    colors.textSecondary,
                )
                BodyCell(if (face.toolCapable) stringResource(Res.string.routing_yes) else "", 1f, colors.success)
            }
            Spacer(Modifier.fillMaxWidth().height(1.dp).background(colors.borderSection))
        }
    }
}

@Composable
private fun RowScope.HeaderCell(text: String, weight: Float) {
    Text(
        text = text,
        style = BoBClawType.monoCaption,
        color = LocalBoBClawColors.textMuted,
        modifier = Modifier.weight(weight),
    )
}

@Composable
private fun RowScope.BodyCell(
    text: String,
    weight: Float,
    color: Color,
    fontWeight: FontWeight = FontWeight.Normal,
) {
    Text(
        text = text,
        style = BoBClawType.monoCaption,
        color = color,
        fontWeight = fontWeight,
        modifier = Modifier.weight(weight),
    )
}
