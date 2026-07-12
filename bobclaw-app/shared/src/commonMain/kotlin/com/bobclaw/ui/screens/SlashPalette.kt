package com.bobclaw.ui.screens

import com.bobclaw.shared.resources.Res
import com.bobclaw.shared.resources.chat_palette_actions
import com.bobclaw.shared.resources.chat_palette_backends
import com.bobclaw.shared.resources.chat_palette_empty
import com.bobclaw.shared.resources.chat_palette_faces
import com.bobclaw.shared.resources.chat_palette_hint
import com.bobclaw.shared.resources.chat_palette_registry_degraded
import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.bobclaw.model.Capabilities
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/**
 * First-run one-click init prompt (U4 "kill-the-blank-canvas"): the `/init` palette action sends
 * this canned first build so a fresh install produces a visible artifact fast — the composer's
 * one-click init. Routed through the normal send path with the currently-pinned face.
 */
internal const val INIT_PROMPT: String =
    "Build me a small self-contained HTML dashboard with three sample metric cards and a simple " +
        "bar chart, then open it in the canvas so I can see BoBClaw working end to end."

/**
 * The chat composer `/` slash palette (UIUX-PLAN §2.4/§4.5, U4). Shown floating above the composer
 * whenever the input starts with `/`; lists the LIVE registry (faces + backends + a one-click init
 * action) served by GET /capabilities (MS8-G1), filtered by whatever the user types after the `/`.
 *
 * Selecting an entry pins that face / backend or runs init — the DropdownMenu-free, keyboard-first
 * discovery surface. Read-only over the registry; the caller owns the state it mutates on pick.
 */
@Composable
internal fun SlashPaletteOverlay(
    query: String,
    capabilities: Capabilities?,
    onPickFace: (String) -> Unit,
    onPickBackend: (String) -> Unit,
    onRunInit: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val entries = remember(capabilities) { buildPaletteEntries(capabilities) }
    val filtered = remember(entries, query) { filterPaletteEntries(entries, query) }

    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(BoBClawShapes.card)
            .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.card)
            .border(1.dp, LocalBoBClawColors.borderCard, BoBClawShapes.card)
            .padding(8.dp),
    ) {
        Text(
            text = stringResource(Res.string.chat_palette_hint),
            color = LocalBoBClawColors.textMuted,
            style = BoBClawType.monoCaption,
            modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp),
        )
        // A partially-degraded registry (a core component was down when /capabilities composed) still
        // lists what it has — surface an honest warn line rather than silently showing a short list.
        if (capabilities?.warnings?.isNotEmpty() == true) {
            Text(
                text = stringResource(Res.string.chat_palette_registry_degraded),
                color = LocalBoBClawColors.warn,
                style = BoBClawType.monoCaption,
                modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp),
            )
        }

        if (filtered.isEmpty()) {
            Text(
                text = stringResource(Res.string.chat_palette_empty),
                color = LocalBoBClawColors.textSecondary,
                style = BoBClawType.label,
                modifier = Modifier.padding(8.dp),
            )
        } else {
            // Cap the height so a long registry scrolls instead of shoving the composer off-screen.
            LazyColumn(modifier = Modifier.fillMaxWidth().heightIn(max = 280.dp)) {
                // Fixed group order (ACTION → FACE → BACKEND); a header per non-empty group.
                for (kind in PaletteKind.values()) {
                    val group = filtered.filter { it.kind == kind }
                    if (group.isEmpty()) continue
                    item(key = "hdr-$kind") { PaletteGroupHeader(kind) }
                    items(group, key = { "${it.kind}-${it.id}" }) { entry ->
                        PaletteRow(entry) {
                            when (entry.kind) {
                                PaletteKind.FACE -> onPickFace(entry.id)
                                PaletteKind.BACKEND -> onPickBackend(entry.id)
                                PaletteKind.ACTION -> onRunInit()
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PaletteGroupHeader(kind: PaletteKind) {
    val label = when (kind) {
        PaletteKind.ACTION -> stringResource(Res.string.chat_palette_actions)
        PaletteKind.FACE -> stringResource(Res.string.chat_palette_faces)
        PaletteKind.BACKEND -> stringResource(Res.string.chat_palette_backends)
    }
    Text(
        text = label,
        color = LocalBoBClawColors.textSecondary,
        style = BoBClawType.monoCaption,
        modifier = Modifier.padding(start = 4.dp, top = 6.dp, bottom = 2.dp),
    )
}

@Composable
private fun PaletteRow(entry: PaletteEntry, onClick: () -> Unit) {
    // A leading sigil keeps the three kinds visually distinct (and hints the machine-vs-face split):
    // `/` action · `@` face · `#` backend. Machine values (backend names) render mono.
    val sigil = when (entry.kind) {
        PaletteKind.ACTION -> "/"
        PaletteKind.FACE -> "@"
        PaletteKind.BACKEND -> "#"
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(BoBClawShapes.control)
            .clickable { onClick() }
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(text = sigil, color = LocalBoBClawColors.accent, style = BoBClawType.monoLabel)
        Spacer(Modifier.width(6.dp))
        Text(
            text = entry.label,
            color = if (entry.available) LocalBoBClawColors.textBody else LocalBoBClawColors.textMuted,
            style = if (entry.kind == PaletteKind.BACKEND) BoBClawType.monoLabel else BoBClawType.body,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        Spacer(Modifier.weight(1f))
        if (entry.detail.isNotEmpty()) {
            Text(
                text = entry.detail,
                color = LocalBoBClawColors.textMuted,
                style = BoBClawType.monoCaption,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
