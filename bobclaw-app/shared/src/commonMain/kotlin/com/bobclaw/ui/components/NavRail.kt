package com.bobclaw.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.bobclaw.ui.RailDest
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/** One rail destination: the enum value, its label, and a placeholder glyph (no icon font this lane). */
private data class RailItem(val dest: RailDest, val label: String, val glyph: String)

/** Top destinations, in order (DESIGN §4: Chat · Council · Teams · Routing · Approvals · Dashboard). */
private val RAIL_ITEMS = listOf(
    RailItem(RailDest.CHAT, "Chat", "💬"),
    RailItem(RailDest.COUNCIL, "Council", "⚖"),
    RailItem(RailDest.TEAMS, "Teams", "👥"),
    RailItem(RailDest.ROUTING, "Routing", "🔀"),
    RailItem(RailDest.APPROVALS, "Approvals", "✅"),
    RailItem(RailDest.DASHBOARD, "Dashboard", "▦"),
)

/**
 * Persistent left navigation rail (DESIGN §4). A narrow vertical [Column] on the [LocalBoBClawColors.rail]
 * surface that hosts every logged-in surface. Top: the destination cells (active = accent / surfaceAccent);
 * Approvals carries a live count badge when > 0. Footer: a health dot (`● OK`, mono), Settings (gear), Logout.
 *
 * Icons are text/emoji glyph placeholders — the bundled Tabler icon font is a later lane (DESIGN §8).
 */
@Composable
fun NavRail(
    selected: RailDest,
    onSelect: (RailDest) -> Unit,
    onLogout: () -> Unit,
    onSettings: () -> Unit,
    approvalsCount: Int = 0,
) {
    val colors = LocalBoBClawColors
    Column(
        modifier = Modifier
            .fillMaxHeight()
            .width(68.dp)
            .background(colors.rail)
            .border(1.dp, colors.borderSection)
            .padding(vertical = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        // --- Destinations (top) ---
        RAIL_ITEMS.forEach { item ->
            RailCell(
                glyph = item.glyph,
                label = item.label,
                active = item.dest == selected,
                badge = if (item.dest == RailDest.APPROVALS && approvalsCount > 0) approvalsCount else null,
                onClick = { onSelect(item.dest) },
            )
            Spacer(Modifier.height(4.dp))
        }

        Spacer(Modifier.weight(1f))

        // --- Footer (bottom): health dot, Settings, Logout ---
        Spacer(
            Modifier
                .padding(horizontal = 10.dp)
                .fillMaxWidth()
                .height(1.dp)
                .background(colors.borderSection),
        )
        Spacer(Modifier.height(8.dp))
        // Health indicator — mono, success dot (static OK; live health wiring is a later lane).
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(text = "●", style = BoBClawType.monoLabel, color = colors.success)
            Text(text = "OK", style = BoBClawType.monoCaption, color = colors.textMuted)
        }
        Spacer(Modifier.height(8.dp))
        RailFooterButton(glyph = "⚙", label = "Settings", onClick = onSettings)
        Spacer(Modifier.height(4.dp))
        RailFooterButton(glyph = "⏻", label = "Logout", onClick = onLogout)
    }
}

/** A top-destination cell: glyph + label, highlighted via accent/surfaceAccent when [active]. */
@Composable
private fun RailCell(
    glyph: String,
    label: String,
    active: Boolean,
    badge: Int?,
    onClick: () -> Unit,
) {
    val colors = LocalBoBClawColors
    val fg = if (active) colors.accent else colors.textSecondary
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 6.dp)
            .clip(BoBClawShapes.cell)
            .then(
                if (active) Modifier.background(colors.surfaceAccent, BoBClawShapes.cell) else Modifier
            )
            .clickable(onClick = onClick)
            .padding(vertical = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(contentAlignment = Alignment.Center) {
            Text(text = glyph, style = BoBClawType.body, color = fg)
            if (badge != null) {
                Box(
                    modifier = Modifier
                        .offset(x = 12.dp, y = (-8).dp)
                        .clip(BoBClawShapes.full)
                        .background(colors.accent, BoBClawShapes.full)
                        .padding(horizontal = 5.dp, vertical = 1.dp),
                ) {
                    Text(
                        text = if (badge > 99) "99+" else badge.toString(),
                        style = BoBClawType.monoCaption,
                        color = colors.onAccent,
                    )
                }
            }
        }
        Spacer(Modifier.height(3.dp))
        Text(
            text = label,
            style = BoBClawType.monoCaption,
            color = fg,
            textAlign = TextAlign.Center,
        )
    }
}

/** A footer action (Settings / Logout): glyph + small label, no active state. */
@Composable
private fun RailFooterButton(glyph: String, label: String, onClick: () -> Unit) {
    val colors = LocalBoBClawColors
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 6.dp)
            .clip(BoBClawShapes.cell)
            .clickable(onClick = onClick)
            .padding(vertical = 4.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(text = glyph, style = BoBClawType.body, color = colors.textSecondary)
        Spacer(Modifier.height(2.dp))
        Text(
            text = label,
            style = BoBClawType.monoCaption,
            color = colors.textMuted,
            textAlign = TextAlign.Center,
        )
    }
}
