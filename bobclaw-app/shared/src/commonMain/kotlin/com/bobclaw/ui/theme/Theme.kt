package com.bobclaw.ui.theme

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

/**
 * BoBClaw "command-center" theme tokens (DESIGN rev 1, 2026-06-19).
 *
 * Replaces the old gradient + glass look. Dark theme is the default and the only
 * fully-specced mode. The accent is a SINGLE source value (default teal) from which
 * [BoBClawColorSet.surfaceAccent], [BoBClawColorSet.borderAccent], and
 * [BoBClawColorSet.accentEmphasis] are derived, so a future user-accent (GUI lane 3)
 * just passes a different `accent` to [bobclawColors] and the whole UI re-skins.
 *
 * Constraints (DESIGN §2): Compose Multiplatform 1.6.11, dependency-free — solid fills,
 * borders, rounded corners only. No blur/backdrop, no gradients-as-structure, commonMain only.
 */

/** Default user-settable accent (DESIGN §3.4 — teal). */
val TealDefault: Color = Color(0xFF2DD4BF)

/**
 * The full §3 token set. Accent-tinted tokens are DERIVED from [accent] in [bobclawColors]
 * so the set re-skins from one user choice.
 */
data class BoBClawColorSet(
    // §3.1 Surfaces
    val canvas: Color,
    val rail: Color,
    val surfaceCard: Color,
    val surfaceRaised: Color,
    val surfaceAccent: Color,   // derived: accent @ ~10% over canvas
    // §3.2 Borders (hairline)
    val borderSection: Color,
    val borderCard: Color,
    val borderControl: Color,
    val borderAccent: Color,    // derived: accent @ ~30% over canvas
    // §3.3 Text
    val textPrimary: Color,
    val textBody: Color,
    val textSecondary: Color,
    val textMuted: Color,
    // §3.4 Accent (user-settable single source + derivations)
    val accent: Color,
    val accentEmphasis: Color,  // derived: accent lightened ~15%
    val onAccent: Color,        // near-black from the accent's own ramp
    // §3.5 Status
    val success: Color,
    val warn: Color,
    val alert: Color,
)

/**
 * Build the §3 color set from a single [accent] (DESIGN §3.4 derivation rules):
 *  - `surfaceAccent` = accent composited over `canvas` at ~10% alpha  → tint for active/selected.
 *  - `borderAccent`  = accent composited over `canvas` at ~30% alpha  → outline on accent-owned cards.
 *  - `accentEmphasis`= accent lightened ~15% toward white (`lerp(accent, White, .15f)`).
 *  - `onAccent`      = a near-black from the accent's own hue ramp (900-stop), i.e. the accent
 *                      pushed almost all the way to black; text/icon ON an accent fill.
 *
 * All derivations are dep-free Compose (`Color.copy(alpha=…).compositeOver(…)` + `lerp`), so any
 * accent the user picks (GUI lane 3) re-derives the whole set. For the default teal these land in
 * the neighborhood of the §3 worked examples (`surfaceAccent ~#15201F`, `borderAccent ~#2A4A45`,
 * `accentEmphasis ~#5EEAD4`, `onAccent ~#06211E`) — the hex in §3 are illustrative; §3.4 specifies
 * these tokens as DERIVED, so the derivation rule is the source of truth, not the example hex.
 */
fun bobclawColors(accent: Color = TealDefault): BoBClawColorSet {
    val canvas = Color(0xFF0F1316)
    return BoBClawColorSet(
        // §3.1 Surfaces
        canvas = canvas,
        rail = Color(0xFF0B0E10),
        surfaceCard = Color(0xFF171B1F),
        surfaceRaised = Color(0xFF1C2126),
        surfaceAccent = accent.copy(alpha = 0.10f).compositeOver(canvas),
        // §3.2 Borders (hairline)
        borderSection = Color(0xFF1E242A),
        borderCard = Color(0xFF262C31),
        borderControl = Color(0xFF2A3138),
        borderAccent = accent.copy(alpha = 0.30f).compositeOver(canvas),
        // §3.3 Text
        textPrimary = Color(0xFFE6EDF1),
        textBody = Color(0xFFC3CDD4),
        textSecondary = Color(0xFF93A1AD),
        textMuted = Color(0xFF5E6B74),
        // §3.4 Accent + derivations
        accent = accent,
        accentEmphasis = lerp(accent, Color.White, 0.15f),
        onAccent = lerp(accent, Color.Black, 0.90f),
        // §3.5 Status
        success = Color(0xFF3FB950),
        warn = Color(0xFFFBBF24),
        alert = Color(0xFFFB923C),
    )
}

/**
 * A user-selectable accent (DESIGN §5 accent picker). [name] is the value PERSISTED to
 * `UserPrefs.accentName`; [label] is the UI text; [color] is the single accent source value fed
 * to [bobclawColors] (which derives `surfaceAccent`/`borderAccent`/`accentEmphasis`/`onAccent`).
 */
data class AccentPreset(val name: String, val label: String, val color: Color)

/**
 * The §5 accent palette — 20 presets across the spectrum. The first entry ("teal") MUST equal
 * [TealDefault] so the default choice round-trips. `name`s are unique, lowercase, persisted keys.
 */
val ACCENT_PRESETS: List<AccentPreset> = listOf(
    AccentPreset("teal",     "Teal",     Color(0xFF2DD4BF)),  // default — equals TealDefault
    AccentPreset("cyan",     "Cyan",     Color(0xFF22D3EE)),
    AccentPreset("aqua",     "Aqua",     Color(0xFF67E8F9)),
    AccentPreset("sky",      "Sky",      Color(0xFF38BDF8)),
    AccentPreset("blue",     "Blue",     Color(0xFF60A5FA)),
    AccentPreset("indigo",   "Indigo",   Color(0xFF818CF8)),
    AccentPreset("violet",   "Violet",   Color(0xFFA78BFA)),
    AccentPreset("purple",   "Purple",   Color(0xFFC084FC)),
    AccentPreset("fuchsia",  "Fuchsia",  Color(0xFFE879F9)),
    AccentPreset("pink",     "Pink",     Color(0xFFF472B6)),
    AccentPreset("rose",     "Rose",     Color(0xFFFB7185)),
    AccentPreset("red",      "Red",      Color(0xFFF87171)),
    AccentPreset("orange",   "Orange",   Color(0xFFFB923C)),
    AccentPreset("amber",    "Amber",    Color(0xFFFBBF24)),
    AccentPreset("yellow",   "Yellow",   Color(0xFFFACC15)),
    AccentPreset("lime",     "Lime",     Color(0xFFA3E635)),
    AccentPreset("green",    "Green",    Color(0xFF4ADE80)),
    AccentPreset("emerald",  "Emerald",  Color(0xFF34D399)),
    AccentPreset("mint",     "Mint",     Color(0xFF6EE7B7)),
    AccentPreset("slate",    "Slate",    Color(0xFF94A3B8)),
)

/** Resolve a persisted accent name to its Color; unknown/blank → teal default. */
fun accentColorFor(name: String): Color =
    ACCENT_PRESETS.firstOrNull { it.name == name }?.color ?: TealDefault

/**
 * Backing CompositionLocal for the active color set, provided at the app root (`App.kt`) from
 * `prefs.accentName`. `static` because the whole set swaps atomically on an accent change — no
 * need to track per-field reads. Seeded with the default (teal) set so previews / tests that
 * never provide it still resolve. `internal` so `App.kt` can `provides` a pref-driven set.
 */
internal val LocalBoBClawColorSet = staticCompositionLocalOf { bobclawColors() }

/**
 * The active color set, read in composable scope. Mirrors the `MaterialTheme.colors` pattern
 * (`@Composable @ReadOnlyComposable get()` over a CompositionLocal) so every existing
 * `LocalBoBClawColors.canvas` / `LocalBoBClawColors.accent` call site keeps compiling UNCHANGED
 * while now resolving the user-chosen accent. (Was a plain top-level `val` in lanes 1–4a.)
 */
val LocalBoBClawColors: BoBClawColorSet
    @Composable @ReadOnlyComposable get() = LocalBoBClawColorSet.current

/**
 * Token surface for the app. Exposes the new §3 tokens AND keeps every legacy
 * `BoBClawColors.*` property name as a backward-compat alias mapping to the nearest new
 * token, so existing screens reskin to the command-center palette with ZERO edits.
 */
object BoBClawColors {
    // The new §3 tokens live on BoBClawColorSet / LocalBoBClawColors (e.g.
    // LocalBoBClawColors.canvas). This object keeps ONLY the legacy aliases so existing
    // screens reskin without edits — exposing the new lowercase names here too would clash
    // on the JVM (`textPrimary` and the legacy `TextPrimary` both mangle to getTextPrimary()).

    // --- Backward-compat aliases (CRITICAL — keep existing screens compiling) ---
    // Old gradient/glass names now map to the nearest command-center token. Verified
    // against every `BoBClawColors.<name>` usage across bobclaw-app. Each getter is now
    // `@Composable @ReadOnlyComposable` so it can read the accent-driven CompositionLocal — the
    // existing composable-scope call sites are untouched; the only NON-composable readers
    // (MarkdownText.inlineAnnotated, the MarkdownParseTest assertion, IdeaInboxTile's top-level
    // input-field color vals) are fixed in lane 4b by threading/inlining the color instead.
    val GradientTop: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.canvas        // was app-bg gradient top → solid canvas
    val GradientBottom: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.rail       // was gradient bottom → rail
    val AccentGreen: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.accent        // single accent (teal default, user-settable)
    val GlassFill: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.surfaceCard     // glass panel fill → solid card surface
    val BorderSubtle: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.borderCard   // hairline outline → card border
    val TextPrimary: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.textPrimary
    val TextSecondary: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.textSecondary
    val KpiGreen: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.success          // healthy/OK metric → status success
    val ZoneHeaderBg: Color @Composable @ReadOnlyComposable get() = LocalBoBClawColors.surfaceRaised // section header band → raised cell
}

/**
 * §3.6 Type & shape tokens.
 *
 * Two families: a sans UI family (default) + a mono family for ALL machine data
 * (IDs, timestamps, backend names, latencies, costs, paths). The mono/sans split is
 * the core of the "command-center" feel. The BUNDLED mono font asset is a later lane —
 * for now the mono styles use [FontFamily.Monospace] so the split exists today.
 *
 * Weights: 400 / 500 / 600 only. Radii: controls/cells 8 · cards 10–12 · pills 20 · dots/avatars full.
 */
object BoBClawShapes {
    /** controls / cells — 8px */
    val control = RoundedCornerShape(8.dp)
    val cell = RoundedCornerShape(8.dp)
    /** cards — 10–12px */
    val card = RoundedCornerShape(12.dp)
    val cardTight = RoundedCornerShape(10.dp)
    /** pills / chips — 20px */
    val pill = RoundedCornerShape(20.dp)
    /** status dots & avatars — fully rounded */
    val full = RoundedCornerShape(percent = 50)
}

object BoBClawType {
    // §3.6 weights — 400 / 500 / 600 only
    val regular = FontWeight.W400
    val medium = FontWeight.W500
    val semibold = FontWeight.W600

    val sans = FontFamily.Default
    // Bundled mono asset arrives in a later lane; the split exists now via Monospace.
    val mono = FontFamily.Monospace

    /** titles / names — 14.5–16, 600 */
    val title = TextStyle(
        fontFamily = sans,
        fontWeight = semibold,
        fontSize = 15.sp,
    )

    /** nav / body copy — 12.5–13.5, 400/500 */
    val body = TextStyle(
        fontFamily = sans,
        fontWeight = regular,
        fontSize = 13.sp,
    )

    /** labels / secondary — 12.5, 500 */
    val label = TextStyle(
        fontFamily = sans,
        fontWeight = medium,
        fontSize = 12.5.sp,
    )

    /** mono labels — IDs / timestamps / backends / latencies / costs — 10–11 */
    val monoLabel = TextStyle(
        fontFamily = mono,
        fontWeight = medium,
        fontSize = 11.sp,
    )

    /** section captions — 10, mono, wide tracking, textMuted (caller applies color) */
    val monoCaption = TextStyle(
        fontFamily = mono,
        fontWeight = regular,
        fontSize = 10.sp,
        letterSpacing = 0.12.em,
    )
}

/**
 * Solid `canvas` background (DESIGN §8 — was a vertical gradient, now flat command-center bg).
 * Same signature as before so callers (`App.kt`, `LoginScreen.kt`, `ChatScreen.kt`) are untouched.
 */
@Composable
fun GradientBackground(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .background(LocalBoBClawColors.canvas)
    ) {
        content()
    }
}

/**
 * Card treatment (DESIGN §8 — was glass/blur/shadow, now a solid card surface + hairline border).
 * Same signature so callers (`Tile.kt`, `ChatScreen.kt`) are untouched. No shadow, no blur.
 *
 * Now `@Composable` (was a plain `Modifier` extension): it reads the accent-driven
 * `LocalBoBClawColors`, which is a composable accessor as of lane 4b. All callers are in
 * composable scope (`Tile.kt`, `ChatScreen.kt`), so this compiles unchanged at the call sites.
 */
@Composable
fun Modifier.glassMorphism(): Modifier = this
    .clip(BoBClawShapes.card)
    .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.card)
    .border(1.dp, LocalBoBClawColors.borderCard, BoBClawShapes.card)
