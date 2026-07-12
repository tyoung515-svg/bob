package com.bobclaw.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.dp
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/**
 * U11 — the inert-but-present voice affordance controls (SPEC-UI-OVERHAUL §7). Rendered ONLY behind
 * the `voice_beta` flag ([voiceAffordancesVisible]); the gating decision lives in the Compose-free
 * [VoiceAffordances] so it stays unit-testable. NO speech engine is wired in v1, so:
 *   · [VoiceMicButton] is DISABLED (muted tint) with a "coming soon" tooltip revealed on tap, and
 *   · [ReadAloudButton] is a no-op per-message placeholder that reveals the same tooltip.
 *
 * The glyphs are built inline here (same Tabler/MIT outline convention as [IconGlyph], kept local so
 * the shared icon registry + its size-locked test stay untouched). App-lane Compose, Opus-authored.
 */

/** A disabled mic affordance for the chat composer + Ask-Bob bubble. Tapping reveals [tooltip]. */
@Composable
fun VoiceMicButton(
    tooltip: String,
    contentDescription: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = micEnabled(),
) {
    val colors = LocalBoBClawColors
    var showTip by remember { mutableStateOf(false) }
    Box(modifier = modifier) {
        Box(
            modifier = Modifier
                .clip(BoBClawShapes.control)
                .background(colors.surfaceCard, BoBClawShapes.control)
                .border(1.dp, colors.borderControl, BoBClawShapes.control)
                // Inert: with no engine this only toggles the "coming soon" hint — it captures nothing.
                .clickable { showTip = !showTip }
                .padding(8.dp),
        ) {
            Icon(
                imageVector = MicVector,
                contentDescription = contentDescription,
                tint = if (enabled) colors.accent else colors.textMuted,
                modifier = Modifier.size(20.dp),
            )
        }
        if (showTip) {
            VoiceTooltip(tooltip, modifier = Modifier.align(Alignment.TopEnd).offset(y = (-34).dp))
        }
    }
}

/** A no-op per-message "read aloud" placeholder (SPEC §7). Tapping reveals [tooltip]; reads nothing. */
@Composable
fun ReadAloudButton(
    label: String,
    tooltip: String,
    modifier: Modifier = Modifier,
    enabled: Boolean = readAloudEnabled(),
) {
    val colors = LocalBoBClawColors
    var showTip by remember { mutableStateOf(false) }
    Box(modifier = modifier) {
        Row(
            modifier = Modifier
                .clip(BoBClawShapes.control)
                .clickable { showTip = !showTip }
                .padding(horizontal = 6.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector = VolumeVector,
                contentDescription = null,
                tint = if (enabled) colors.accent else colors.textMuted,
                modifier = Modifier.size(13.dp),
            )
            Spacer(Modifier.width(4.dp))
            Text(label, color = colors.textMuted, style = BoBClawType.monoCaption)
        }
        if (showTip) {
            VoiceTooltip(tooltip, modifier = Modifier.align(Alignment.TopEnd).offset(y = (-30).dp))
        }
    }
}

/** A small floating "coming soon" tooltip bubble anchored above its trigger. */
@Composable
private fun VoiceTooltip(text: String, modifier: Modifier = Modifier) {
    val colors = LocalBoBClawColors
    Box(
        modifier = modifier
            .clip(BoBClawShapes.control)
            .background(colors.surfaceRaised, BoBClawShapes.control)
            .border(1.dp, colors.borderControl, BoBClawShapes.control)
            .padding(horizontal = 8.dp, vertical = 4.dp),
    ) {
        Text(text, color = colors.textBody, style = BoBClawType.monoCaption)
    }
}

// ── inline Tabler outline glyphs (MIT) — 24×24, 2px round stroke (mirrors IconGlyph.strokeIcon) ────
private fun voiceGlyph(name: String, vararg paths: String): ImageVector {
    val b = ImageVector.Builder(
        name = name,
        defaultWidth = 24.dp,
        defaultHeight = 24.dp,
        viewportWidth = 24f,
        viewportHeight = 24f,
    )
    for (d in paths) {
        b.addPath(
            pathData = PathParser().parsePathString(d).toNodes(),
            stroke = SolidColor(Color.Black), // recolored by Icon(tint=…) at draw time
            strokeLineWidth = 2f,
            strokeLineCap = StrokeCap.Round,
            strokeLineJoin = StrokeJoin.Round,
        )
    }
    return b.build()
}

/** Tabler `microphone`. */
private val MicVector: ImageVector by lazy {
    voiceGlyph(
        "microphone",
        "M9 2m0 3a3 3 0 0 1 3 -3h0a3 3 0 0 1 3 3v5a3 3 0 0 1 -3 3h0a3 3 0 0 1 -3 -3z",
        "M5 10a7 7 0 0 0 14 0",
        "M8 21l8 0",
        "M12 17l0 4",
    )
}

/** Tabler `volume` (read-aloud). */
private val VolumeVector: ImageVector by lazy {
    voiceGlyph(
        "volume",
        "M15 8a5 5 0 0 1 0 8",
        "M17.7 5a9 9 0 0 1 0 14",
        "M6 15h-2a1 1 0 0 1 -1 -1v-4a1 1 0 0 1 1 -1h2l3.5 -4.5a0.8 .8 0 0 1 1.5 .5v14a0.8 .8 0 0 1 -1.5 .5l-3.5 -4.5",
    )
}
