package com.bobclaw.ui.screens

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/**
 * A one-shot imperative command to the embedded 3D graph (Kotlin → JS), applied once per
 * [nonce] so re-composition never re-fires it. [Kind.FLY_TO] drives search-and-fly-to;
 * [Kind.REMOVE] drives the incremental node removal after a Forget round-trip.
 */
data class GraphOneShot(val nonce: Long, val kind: Kind, val nodeId: String) {
    enum class Kind { FLY_TO, REMOVE }
}

/**
 * The platform seam that renders the memory graph (desktop = JCEF embedded-Chromium canvas;
 * other targets = [PlaceholderGraphRenderer]). Injected from the platform entrypoint exactly
 * like `artifactRenderer`, so commonMain never imports JCEF.
 *
 *  - [graphJson]      : the current (filtered) graph as compact JSON → JS `BClaw.render` on change.
 *  - [oneShot]        : fly-to / remove, applied once per identity (search + forget).
 *  - [onNodeSelected] : JS → Kotlin node-click callback (opens the inspect panel).
 */
typealias MemoryGraphRenderer = @Composable (
    graphJson: String?,
    oneShot: GraphOneShot?,
    onNodeSelected: (String) -> Unit,
    modifier: Modifier,
) -> Unit

/** Fallback renderer for platforms without an embedded browser (android / ios / unit tests). */
@Composable
fun PlaceholderGraphRenderer(
    graphJson: String?,
    oneShot: GraphOneShot?,
    onNodeSelected: (String) -> Unit,
    modifier: Modifier,
) {
    val colors = LocalBoBClawColors
    Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text(
            text = "3D graph is available in the desktop app.",
            style = BoBClawType.monoCaption,
            color = colors.textMuted,
        )
    }
}
