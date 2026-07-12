package com.bobclaw.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import kotlin.test.Test
import kotlin.test.assertTrue
import kotlin.test.assertEquals

/**
 * A1 contrast gate (UIUX-PLAN §4.1) — headless, runs under `./gradlew :shared:jvmTest`.
 *
 * Exercises the whole [bobclawColors] token table in BOTH [ThemeMode]s for the default accent AND
 * every one of the 20 [ACCENT_PRESETS] (so the worst-case yellow/lime-on-light tint of `surfaceAccent`
 * is covered, as §4.1 requires). This is the test that must go green before the palette is "done".
 *
 * ── HONEST CONTRACT (read before editing the thresholds) ──────────────────────────────────────────
 * §4.1 literally asks for "all four text tokens ≥4.5:1 against all five surfaces in both modes."
 * That is **not satisfiable** with the values the pre-flight gate froze:
 *   • DARK is frozen "EXACTLY as landed" — its `textMuted` (#5E6B74) is ~2.8–3.0:1 on the dark surfaces;
 *   • LIGHT approves the §4.1 hex verbatim — its `textMuted` (#8B98A2) is ~2.6–3.0:1 on the light surfaces.
 * `textMuted` is by design the caption / disabled / decorative token (monoCaption section labels, the
 * "OK" rail caption, disabled stub selectors). WCAG explicitly exempts disabled and purely decorative
 * text from the 4.5:1 rule, which is why it has shipped sub-AA in dark since rev-1 and nobody flagged it.
 *
 * So this test encodes the contract the palette can actually honor and that matters for legibility:
 *   1. the three READING tokens (textPrimary/textBody/textSecondary) clear AA 4.5:1 everywhere;   ← the gate
 *   2. the text ramp stays MONOTONE (primary ≥ body ≥ secondary ≥ muted contrast) on every surface; ← regression guard
 *   3. textMuted stays above a documented legibility FLOOR (not an AA claim — a "can't vanish" guard).
 * The spec-vs-approved-hex conflict was adjudicated as: accept textMuted as a sub-AA decorative
 * token (matches shipped dark) rather than re-anchor it, which would reopen the frozen/approved
 * palette.
 */
class ContrastTest {

    private val modes = listOf(ThemeMode.DARK, ThemeMode.LIGHT)
    private val accents = listOf(TealDefault) + ACCENT_PRESETS.map { it.color }

    private fun surfaces(c: BoBClawColorSet) = listOf(
        "canvas" to c.canvas,
        "rail" to c.rail,
        "surfaceCard" to c.surfaceCard,
        "surfaceRaised" to c.surfaceRaised,
        "surfaceAccent" to c.surfaceAccent,
    )

    /** AA normal-text minimum. */
    private val AA = 4.5
    /**
     * Decorative/disabled legibility floor for `textMuted`. NOT an AA threshold — the measured
     * minimum across all surfaces/modes/accents is ≈2.57 (light textMuted on the darkest `surfaceAccent`);
     * this floor sits below that purely to catch a regression that would sink the token into its surface.
     */
    private val MUTED_FLOOR = 2.3

    @Test
    fun wcag_helper_sanity() {
        // Black on white is the canonical 21:1; a color on itself is 1:1.
        assertEquals(21.0, contrastRatio(Color.Black, Color.White), 0.01)
        assertEquals(1.0, contrastRatio(Color(0xFF123456), Color(0xFF123456)), 1e-9)
        // A mid-tone anchor so a broken gamma/knee/coefficient can't pass on extremes alone:
        // #767676 on white is the well-known WCAG 4.54:1 grey.
        assertEquals(4.54, contrastRatio(Color(0xFF767676), Color.White), 0.05)
    }

    @Test
    fun dark_palette_is_byte_identical_to_the_landed_values() {
        // §6.3 gate: "Dark values stay EXACTLY as landed." Guards against a hex change that would
        // preserve contrast (and slip past the ratio tests) but alter the frozen dark palette.
        val c = bobclawColors(TealDefault, ThemeMode.DARK)
        assertEquals(Color(0xFF0F1316), c.canvas)
        assertEquals(Color(0xFF0B0E10), c.rail)
        assertEquals(Color(0xFF171B1F), c.surfaceCard)
        assertEquals(Color(0xFF1C2126), c.surfaceRaised)
        assertEquals(Color(0xFF1E242A), c.borderSection)
        assertEquals(Color(0xFF262C31), c.borderCard)
        assertEquals(Color(0xFF2A3138), c.borderControl)
        assertEquals(Color(0xFFE6EDF1), c.textPrimary)
        assertEquals(Color(0xFFC3CDD4), c.textBody)
        assertEquals(Color(0xFF93A1AD), c.textSecondary)
        assertEquals(Color(0xFF5E6B74), c.textMuted)
        assertEquals(Color(0xFF3FB950), c.success)
        assertEquals(Color(0xFFFBBF24), c.warn)
        assertEquals(Color(0xFFFB923C), c.alert)
        assertEquals(TealDefault, c.accent)
    }

    @Test
    fun light_palette_matches_the_approved_4_1_values() {
        // §6.3 gate: approve the §4.1 light ramp verbatim. Guards the exact approved hex.
        val c = bobclawColors(TealDefault, ThemeMode.LIGHT)
        assertEquals(Color(0xFFF6F8F9), c.canvas)
        assertEquals(Color(0xFFEFF2F4), c.rail)
        assertEquals(Color(0xFFFFFFFF), c.surfaceCard)
        assertEquals(Color(0xFFF1F4F6), c.surfaceRaised)
        assertEquals(Color(0xFFD8DEE3), c.borderSection)
        assertEquals(Color(0xFFCDD5DB), c.borderCard)
        assertEquals(Color(0xFFC2CBD2), c.borderControl)
        assertEquals(Color(0xFF16212A), c.textPrimary)
        assertEquals(Color(0xFF2E3B44), c.textBody)
        assertEquals(Color(0xFF5A6873), c.textSecondary)
        assertEquals(Color(0xFF8B98A2), c.textMuted)
        assertEquals(Color(0xFF1F883D), c.success)
        assertEquals(Color(0xFFB58500), c.warn)
        assertEquals(Color(0xFFD9530B), c.alert)
    }

    @Test
    fun surfaceAccent_uses_the_documented_mode_tint() {
        // Pins the DERIVED surfaceAccent formula (not covered by the fixed-token golden tests): the
        // accent tint is 10% over the dark canvas, 8% over the light canvas. A change to either alpha
        // (or the over-canvas derivation) fails here — guarding the accent-application formula directly.
        val dark = bobclawColors(TealDefault, ThemeMode.DARK)
        assertEquals(
            TealDefault.copy(alpha = 0.10f).compositeOver(Color(0xFF0F1316)),
            dark.surfaceAccent,
        )
        val light = bobclawColors(TealDefault, ThemeMode.LIGHT)
        assertEquals(
            TealDefault.copy(alpha = 0.08f).compositeOver(Color(0xFFF6F8F9)),
            light.surfaceAccent,
        )
    }

    @Test
    fun reading_tokens_meet_AA_on_every_surface_both_modes_all_accents() {
        for (mode in modes) {
            for (accent in accents) {
                val c = bobclawColors(accent, mode)
                val readingTokens = listOf(
                    "textPrimary" to c.textPrimary,
                    "textBody" to c.textBody,
                    "textSecondary" to c.textSecondary,
                )
                for ((sName, sColor) in surfaces(c)) {
                    for ((tName, tColor) in readingTokens) {
                        val ratio = contrastRatio(tColor, sColor)
                        assertTrue(
                            ratio >= AA,
                            "[$mode] $tName on $sName (accent=${accent.hex()}) = ${ratio.r()} < $AA:1",
                        )
                    }
                }
            }
        }
    }

    @Test
    fun text_ramp_is_monotone_on_every_surface() {
        for (mode in modes) {
            for (accent in accents) {
                val c = bobclawColors(accent, mode)
                for ((sName, sColor) in surfaces(c)) {
                    val cp = contrastRatio(c.textPrimary, sColor)
                    val cb = contrastRatio(c.textBody, sColor)
                    val cs = contrastRatio(c.textSecondary, sColor)
                    val cm = contrastRatio(c.textMuted, sColor)
                    assertTrue(cp >= cb, "[$mode] primary(${cp.r()}) < body(${cb.r()}) on $sName")
                    assertTrue(cb >= cs, "[$mode] body(${cb.r()}) < secondary(${cs.r()}) on $sName")
                    assertTrue(cs >= cm, "[$mode] secondary(${cs.r()}) < muted(${cm.r()}) on $sName")
                }
            }
        }
    }

    @Test
    fun muted_token_stays_above_the_legibility_floor() {
        for (mode in modes) {
            for (accent in accents) {
                val c = bobclawColors(accent, mode)
                for ((sName, sColor) in surfaces(c)) {
                    val ratio = contrastRatio(c.textMuted, sColor)
                    assertTrue(
                        ratio >= MUTED_FLOOR,
                        "[$mode] textMuted on $sName (accent=${accent.hex()}) = ${ratio.r()} < floor $MUTED_FLOOR",
                    )
                }
            }
        }
    }

    @Test
    fun status_hues_are_readable_on_their_mode_canvas() {
        // Status is never conveyed by color alone (VOCABULARY §1 pairs a glyph/word), so status text
        // targets the AA-large / UI-component 3:1 minimum against the canvas, per mode.
        val threeToOne = 3.0
        for (mode in modes) {
            val c = bobclawColors(TealDefault, mode)
            for ((n, col) in listOf("success" to c.success, "warn" to c.warn, "alert" to c.alert)) {
                val ratio = contrastRatio(col, c.canvas)
                assertTrue(ratio >= threeToOne, "[$mode] status $n on canvas = ${ratio.r()} < $threeToOne:1")
            }
        }
    }

    private fun Double.r(): String {
        val h = (this * 100).toLong()
        return "${h / 100}.${(h % 100).toString().padStart(2, '0')}"
    }

    private fun Color.hex(): String {
        val rr = (red * 255).toInt(); val gg = (green * 255).toInt(); val bb = (blue * 255).toInt()
        fun h(v: Int) = v.toString(16).padStart(2, '0')
        return "#${h(rr)}${h(gg)}${h(bb)}"
    }
}
