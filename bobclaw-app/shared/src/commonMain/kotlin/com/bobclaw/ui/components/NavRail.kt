package com.bobclaw.ui.components

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.bobclaw.ui.RailDest
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/** One rail destination: the enum value + its [IconGlyph] name (Tabler subset — no emoji).
 * The label is resolved separately (see [railLabel]) so this list stays a plain, testable data
 * structure — a `@Composable` can't be a top-level val, and tests assert the order/icons here. */
data class RailItem(val dest: RailDest, val iconName: String)

/** Top destinations, in order — MS9 U1 / SPEC §2 D1:
 * **Home · Chat · Council · Teams · Memory · Approvals** (Settings + Logout live in the footer,
 * NOT here). Routing's top-level destination is retired: its table moved into a Teams tab (U1).
 * Icon names map to the bundled Tabler subset per ASSET-MANIFEST §1 (emoji fully retired). */
val RAIL_ITEMS: List<RailItem> = listOf(
    RailItem(RailDest.HOME, "home"),
    RailItem(RailDest.CHAT, "message-circle"),
    RailItem(RailDest.COUNCIL, "scale"),
    RailItem(RailDest.TEAMS, "users"),
    RailItem(RailDest.MEMORY, "brain"),
    RailItem(RailDest.APPROVALS, "checks"),
)

/** Localized rail label for a destination, resolved at render (keeps [RAIL_ITEMS] pure). */
@Composable
private fun railLabel(dest: RailDest): String = when (dest) {
    RailDest.HOME -> stringResource(Res.string.nav_home)
    RailDest.CHAT -> stringResource(Res.string.nav_chat)
    RailDest.COUNCIL -> stringResource(Res.string.nav_council)
    RailDest.TEAMS -> stringResource(Res.string.nav_teams)
    RailDest.MEMORY -> stringResource(Res.string.nav_memory)
    RailDest.APPROVALS -> stringResource(Res.string.nav_approvals)
}

/**
 * Persistent left navigation rail (DESIGN §4). A narrow vertical [Column] on the [LocalBoBClawColors.rail]
 * surface that hosts every logged-in surface. Top: the destination cells (active = accent / surfaceAccent);
 * Approvals carries a live count badge when > 0. Footer: a health dot + OK caption, Settings, Logout.
 *
 * Icons are the bundled Tabler [IconGlyph] subset (ASSET-MANIFEST §1) — no emoji anywhere in the rail.
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
                iconName = item.iconName,
                label = railLabel(item.dest),
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
        // Health indicator — success status dot (§4.4: glyph + word, never color alone); static OK,
        // live health wiring is a later lane. The dot is the bundled Tabler `point-filled` (no emoji).
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            IconGlyph(name = "point-filled", tint = colors.success, size = 10.dp)
            Text(text = stringResource(Res.string.nav_ok), style = BoBClawType.monoCaption, color = colors.textMuted)
        }
        Spacer(Modifier.height(8.dp))
        RailFooterButton(iconName = "settings", label = stringResource(Res.string.nav_settings), onClick = onSettings)
        Spacer(Modifier.height(4.dp))
        RailFooterButton(iconName = "power", label = stringResource(Res.string.nav_logout), onClick = onLogout)
    }
}

/** A top-destination cell: icon + label, highlighted via accent/surfaceAccent when [active]. */
@Composable
private fun RailCell(
    iconName: String,
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
            IconGlyph(name = iconName, tint = fg, size = 22.dp, contentDescription = label)
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
        // Label-wrap fix (U1): single line, never mid-word wrap ("Approva ls" / "Dashboa rd").
        // softWrap=false + ellipsis clips instead of breaking a word across two lines.
        Text(
            text = label,
            style = BoBClawType.monoCaption,
            color = fg,
            textAlign = TextAlign.Center,
            maxLines = 1,
            softWrap = false,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** A footer action (Settings / Logout): icon + small label, no active state. */
@Composable
private fun RailFooterButton(iconName: String, label: String, onClick: () -> Unit) {
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
        IconGlyph(name = iconName, tint = colors.textSecondary, size = 20.dp, contentDescription = label)
        Spacer(Modifier.height(2.dp))
        Text(
            text = label,
            style = BoBClawType.monoCaption,
            color = colors.textMuted,
            textAlign = TextAlign.Center,
            maxLines = 1,
            softWrap = false,
            overflow = TextOverflow.Ellipsis,
        )
    }
}
