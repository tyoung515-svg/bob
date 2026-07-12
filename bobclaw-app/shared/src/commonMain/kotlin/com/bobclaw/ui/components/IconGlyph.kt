package com.bobclaw.ui.components

import androidx.compose.foundation.layout.size
import androidx.compose.material3.Icon
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * The shared icon component (UIUX-PLAN §3.2 / ASSET-MANIFEST §1) — the app's ONE indirection from a
 * stable icon `name` to a monochrome, tintable glyph. This is what retires the emoji nav icons
 * (finding A8): `IconGlyph("message-circle")` replaces the old chat emoji. **The app never uses emoji.**
 *
 * Delivery: the Tabler (MIT) subset is embedded as inline [ImageVector]s built from the upstream
 * outline path data (24×24 viewport, 2px round stroke — Tabler's own spec). This is a deliberate,
 * more-robust variant of the manifest's "SVG in drawable/": no Compose-Resources vector-XML pipeline
 * to depend on, crisp at any UI-scale, tinted at draw time via [Icon]. The MIT notice ships in
 * `composeResources/files/licenses/Tabler-MIT.txt`. Adding an icon = one entry in [tablerIcon].
 */
@Composable
fun IconGlyph(
    name: String,
    modifier: Modifier = Modifier,
    tint: Color = Color.Unspecified,
    size: Dp = 20.dp,
    contentDescription: String? = null,
) {
    Icon(
        imageVector = tablerIcon(name),
        contentDescription = contentDescription,
        tint = tint,
        modifier = modifier.size(size),
    )
}

/** Every icon name the app knows → its embedded vector. Unknown names fall back to a filled dot. */
fun tablerIcon(name: String): ImageVector = when (name) {
    "message-circle" -> IconMessageCircle
    "scale" -> IconScale
    "users" -> IconUsers
    "arrows-split" -> IconArrowsSplit
    "checks" -> IconChecks
    "layout-dashboard" -> IconLayoutDashboard
    "home" -> IconHome
    "brain" -> IconBrain
    "clock" -> IconClock
    "settings" -> IconSettings
    "power" -> IconPower
    "point-filled" -> IconPointFilled
    "tool" -> IconTool
    "arrow-bounce" -> IconArrowBounce
    "satellite" -> IconSatellite
    "x" -> IconX
    else -> IconPointFilled
}

/** True iff [name] maps to a real bundled glyph (not the fallback). Used by tests + audits. */
fun isKnownIcon(name: String): Boolean = name in KNOWN_ICON_NAMES

val KNOWN_ICON_NAMES: Set<String> = setOf(
    "message-circle", "scale", "users", "arrows-split", "checks", "layout-dashboard",
    "home", "brain", "clock",
    "settings", "power", "point-filled", "tool", "arrow-bounce", "satellite", "x",
)

// ── inline Tabler outline vectors (MIT) — 24×24, 2px round stroke ────────────────────────────────

private fun strokeIcon(name: String, vararg paths: String): ImageVector {
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

private val IconMessageCircle: ImageVector by lazy {
    strokeIcon(
        "message-circle",
        "M3 20l1.3 -3.9c-2.324 -3.437 -1.426 -7.872 2.1 -10.374c3.526 -2.501 8.59 -2.296 11.845 .48c3.255 2.777 3.695 7.266 1.029 10.501c-2.666 3.235 -7.615 4.215 -11.574 2.293l-4.7 1",
    )
}

private val IconScale: ImageVector by lazy {
    strokeIcon(
        "scale",
        "M7 20l10 0",
        "M6 6l6 -1l6 1",
        "M12 3l0 17",
        "M9 12l-3 -6l-3 6a3 3 0 0 0 6 0",
        "M21 12l-3 -6l-3 6a3 3 0 0 0 6 0",
    )
}

private val IconUsers: ImageVector by lazy {
    strokeIcon(
        "users",
        "M5 7a4 4 0 1 0 8 0a4 4 0 1 0 -8 0",
        "M3 21v-2a4 4 0 0 1 4 -4h4a4 4 0 0 1 4 4v2",
        "M16 3.13a4 4 0 0 1 0 7.75",
        "M21 21v-2a4 4 0 0 0 -3 -3.85",
    )
}

private val IconArrowsSplit: ImageVector by lazy {
    strokeIcon(
        "arrows-split",
        "M21 17h-8l-3.5 -5h-6.5",
        "M21 7h-8l-3.495 5",
        "M18 10l3 -3l-3 -3",
        "M18 20l3 -3l-3 -3",
    )
}

private val IconChecks: ImageVector by lazy {
    strokeIcon(
        "checks",
        "M7 12l5 5l10 -10",
        "M2 12l5 5m5 -5l5 -5",
    )
}

private val IconLayoutDashboard: ImageVector by lazy {
    strokeIcon(
        "layout-dashboard",
        "M5 4h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1",
        "M5 16h4a1 1 0 0 1 1 1v2a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-2a1 1 0 0 1 1 -1",
        "M15 12h4a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1",
        "M15 4h4a1 1 0 0 1 1 1v2a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1v-2a1 1 0 0 1 1 -1",
    )
}

private val IconHome: ImageVector by lazy {
    strokeIcon(
        "home",
        "M5 12l-2 0l9 -9l9 9l-2 0",
        "M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-7",
        "M9 21v-6a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v6",
    )
}

private val IconBrain: ImageVector by lazy {
    strokeIcon(
        "brain",
        "M15.5 13a3.5 3.5 0 0 0 -3.5 3.5v1a3.5 3.5 0 0 0 7 0v-1.8",
        "M8.5 13a3.5 3.5 0 0 1 3.5 3.5v1a3.5 3.5 0 0 1 -7 0v-1.8",
        "M17.5 16a3.5 3.5 0 0 0 0 -7h-.5",
        "M19 9.3v-.3a4 4 0 0 0 -4 -4a4.5 4.5 0 0 0 -3 1.2a4.5 4.5 0 0 0 -3 -1.2a4 4 0 0 0 -4 4v.3",
        "M6.5 16a3.5 3.5 0 0 0 0 -7h.5",
        "M12 5.7v13.3",
    )
}

private val IconClock: ImageVector by lazy {
    strokeIcon(
        "clock",
        "M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0",
        "M12 7v5l3 3",
    )
}

private val IconSettings: ImageVector by lazy {
    strokeIcon(
        "settings",
        "M10.325 4.317c.426 -1.756 2.924 -1.756 3.35 0a1.724 1.724 0 0 0 2.573 1.066c1.543 -.94 3.31 .826 2.37 2.37a1.724 1.724 0 0 0 1.065 2.572c1.756 .426 1.756 2.924 0 3.35a1.724 1.724 0 0 0 -1.066 2.573c.94 1.543 -.826 3.31 -2.37 2.37a1.724 1.724 0 0 0 -2.572 1.065c-.426 1.756 -2.924 1.756 -3.35 0a1.724 1.724 0 0 0 -2.573 -1.066c-1.543 .94 -3.31 -.826 -2.37 -2.37a1.724 1.724 0 0 0 -1.065 -2.572c-1.756 -.426 -1.756 -2.924 0 -3.35a1.724 1.724 0 0 0 1.066 -2.573c-.94 -1.543 .826 -3.31 2.37 -2.37c1 .608 2.296 .07 2.572 -1.065",
        "M9 12a3 3 0 1 0 6 0a3 3 0 0 0 -6 0",
    )
}

private val IconPower: ImageVector by lazy {
    strokeIcon(
        "power",
        "M7 6a7.75 7.75 0 1 0 10 0",
        "M12 4l0 8",
    )
}

private val IconX: ImageVector by lazy {
    strokeIcon(
        "x",
        "M18 6l-12 12",
        "M6 6l12 12",
    )
}

private val IconTool: ImageVector by lazy {
    strokeIcon(
        "tool",
        "M7 10h3v-3l-3.5 -3.5a6 6 0 0 1 8 8l6 6a2 2 0 0 1 -3 3l-6 -6a6 6 0 0 1 -8 -8l3.5 3.5",
    )
}

private val IconArrowBounce: ImageVector by lazy {
    strokeIcon(
        "arrow-bounce",
        "M10 18h4",
        "M3 8a9 9 0 0 1 9 9v1l1.428 -4.285a12 12 0 0 1 6.018 -6.938l.554 -.277",
        "M15 6h5v5",
    )
}

private val IconSatellite: ImageVector by lazy {
    strokeIcon(
        "satellite",
        "M3.707 6.293l2.586 -2.586a1 1 0 0 1 1.414 0l5.586 5.586a1 1 0 0 1 0 1.414l-2.586 2.586a1 1 0 0 1 -1.414 0l-5.586 -5.586a1 1 0 0 1 0 -1.414",
        "M6 10l-3 3l3 3l3 -3",
        "M10 6l3 -3l3 3l-3 3",
        "M12 12l1.5 1.5",
        "M14.5 17a2.5 2.5 0 0 0 2.5 -2.5",
        "M15 21a6 6 0 0 0 6 -6",
    )
}

/** point-filled is Tabler's only FILLED glyph in our subset — a solid dot (health/status marker). */
private val IconPointFilled: ImageVector by lazy {
    ImageVector.Builder(
        name = "point-filled",
        defaultWidth = 24.dp,
        defaultHeight = 24.dp,
        viewportWidth = 24f,
        viewportHeight = 24f,
    ).apply {
        path(fill = SolidColor(Color.Black)) {
            // circle centered (12,12), r=4
            moveTo(16f, 12f)
            arcToRelative(4f, 4f, 0f, true, false, -8f, 0f)
            arcToRelative(4f, 4f, 0f, true, false, 8f, 0f)
            close()
        }
    }.build()
}
