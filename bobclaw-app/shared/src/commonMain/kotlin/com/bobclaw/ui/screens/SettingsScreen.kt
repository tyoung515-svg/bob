package com.bobclaw.ui.screens

import org.jetbrains.compose.resources.StringResource

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.bobclaw.network.UI_SCALE_MAX
import com.bobclaw.network.UI_SCALE_MIN
import com.bobclaw.network.UserPrefs
import com.bobclaw.ui.theme.ACCENT_PRESETS
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import kotlin.math.round

/** The Settings sub-nav sections (DESIGN §5). Only [Pane.APPEARANCE] is functional in lane 4a. */
private enum class Pane(val labelRes: StringResource) {
    APPEARANCE(Res.string.settings_pane_appearance),
    ACCOUNT(Res.string.settings_pane_account),
    MODELS(Res.string.settings_pane_models),
    CONNECTIONS(Res.string.settings_pane_connections),
    APPROVALS(Res.string.settings_pane_approvals),
    ADVANCED(Res.string.settings_pane_advanced),
}

/** UI-scale stepper increment (0.05× per tap — DESIGN §5). */
private const val UI_SCALE_STEP = 0.05f

/**
 * Settings surface (DESIGN §5): a left sub-nav + a right detail pane, styled with the
 * command-center tokens. Most panes are simple "Not yet configured" placeholders; the one
 * functional pane is **Appearance**, which exposes a working UI-scale stepper (0.8×–1.5×).
 *
 * State is hoisted in `App.kt`: changing the UI-scale control calls
 * `onPrefsChange(prefs.copy(uiScale = newValue))`; App persists + re-renders, and the root
 * `LocalDensity` override applies the new scale app-wide.
 *
 * @param prefs the current persisted prefs (the UI-scale row reflects [UserPrefs.uiScale]).
 * @param onPrefsChange invoked with the next prefs whenever the user changes a functional control.
 */
@Composable
fun SettingsScreen(
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
    modifier: Modifier = Modifier,
) {
    var pane by remember { mutableStateOf(Pane.APPEARANCE) }
    GradientBackground(modifier = modifier) {
        Row(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            // ---- left sub-nav ----
            SettingsSubNav(
                selected = pane,
                onSelect = { pane = it },
                modifier = Modifier.width(220.dp).fillMaxHeight(),
            )
            Spacer(Modifier.width(16.dp))
            // ---- right detail pane ----
            Box(modifier = Modifier.weight(1f).fillMaxHeight()) {
                when (pane) {
                    Pane.APPEARANCE -> AppearancePane(prefs = prefs, onPrefsChange = onPrefsChange)
                    else -> NotConfiguredPane(stringResource(pane.labelRes))
                }
            }
        }
    }
}

@Composable
private fun SettingsSubNav(
    selected: Pane,
    onSelect: (Pane) -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = LocalBoBClawColors
    Column(
        modifier = modifier
            .clip(BoBClawShapes.card)
            .background(colors.surfaceCard, BoBClawShapes.card)
            .border(1.dp, colors.borderCard, BoBClawShapes.card)
            .padding(12.dp),
    ) {
        Text(
            text = stringResource(Res.string.settings_nav_title),
            style = BoBClawType.monoCaption,
            color = colors.textMuted,
        )
        Spacer(Modifier.height(12.dp))
        Pane.entries.forEach { entry ->
            val isSelected = entry == selected
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(BoBClawShapes.control)
                    .background(
                        if (isSelected) colors.surfaceAccent else Color.Transparent,
                        BoBClawShapes.control,
                    )
                    .clickable { onSelect(entry) }
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = stringResource(entry.labelRes),
                    style = BoBClawType.label,
                    color = if (isSelected) colors.accent else colors.textBody,
                )
            }
            Spacer(Modifier.height(2.dp))
        }
    }
}

@Composable
private fun AppearancePane(
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState()),
    ) {
        SettingsCard(caption = stringResource(Res.string.settings_interface)) {
            UiScaleRow(prefs = prefs, onPrefsChange = onPrefsChange)
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_accent)) {
            // Lane 4b — the user-settable accent picker. Writes prefs.accentName; App re-derives the
            // whole color set from it and the entire app re-skins live (and the choice persists).
            AccentPickerRow(prefs = prefs, onPrefsChange = onPrefsChange)
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_theme)) {
            // Stubbed/disabled per DESIGN §5 — visually present, not wired (no onPrefsChange).
            StubSelectorRow(
                title = stringResource(Res.string.settings_theme_title),
                options = listOf(stringResource(Res.string.settings_dark), stringResource(Res.string.settings_light), stringResource(Res.string.settings_system)),
                selectedLabel = stringResource(Res.string.settings_dark),
            )
            Spacer(Modifier.height(12.dp))
            StubSelectorRow(
                title = stringResource(Res.string.settings_density_title),
                options = listOf(stringResource(Res.string.settings_comfortable), stringResource(Res.string.settings_compact)),
                selectedLabel = stringResource(Res.string.settings_comfortable),
            )
        }
    }
}

/** The one functional control: a UI-scale stepper, 0.8×–1.5×, current value shown in mono. */
@Composable
private fun UiScaleRow(
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
) {
    val colors = LocalBoBClawColors
    // Round to the 0.05 grid so repeated steps don't drift on float arithmetic.
    fun setScale(raw: Float) {
        val snapped = round(raw / UI_SCALE_STEP) * UI_SCALE_STEP
        val clamped = snapped.coerceIn(UI_SCALE_MIN, UI_SCALE_MAX)
        if (clamped != prefs.uiScale) onPrefsChange(prefs.copy(uiScale = clamped))
    }

    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.weight(1f)) {
                Text(stringResource(Res.string.settings_ui_scale), style = BoBClawType.title, color = colors.textPrimary)
                Spacer(Modifier.height(2.dp))
                Text(
                    stringResource(Res.string.settings_ui_scale_description),
                    style = BoBClawType.body,
                    color = colors.textSecondary,
                )
            }
            StepperButton(symbol = "−", enabled = prefs.uiScale > UI_SCALE_MIN) {
                setScale(prefs.uiScale - UI_SCALE_STEP)
            }
            Spacer(Modifier.width(10.dp))
            // current value in mono — machine value per the theme's mono/sans split
            Box(
                modifier = Modifier
                    .width(64.dp)
                    .clip(BoBClawShapes.control)
                    .background(colors.surfaceRaised, BoBClawShapes.control)
                    .border(1.dp, colors.borderControl, BoBClawShapes.control)
                    .padding(vertical = 7.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = formatScale(prefs.uiScale),
                    style = BoBClawType.monoLabel,
                    color = colors.textPrimary,
                )
            }
            Spacer(Modifier.width(10.dp))
            StepperButton(symbol = "+", enabled = prefs.uiScale < UI_SCALE_MAX) {
                setScale(prefs.uiScale + UI_SCALE_STEP)
            }
        }
        Spacer(Modifier.height(10.dp))
        // Reset-to-100% affordance (nice-to-have per DESIGN §5).
        Text(
            text = stringResource(Res.string.settings_reset_to_100),
            style = BoBClawType.label,
            color = if (prefs.uiScale != 1.0f) colors.accent else colors.textMuted,
            modifier = Modifier
                .clip(BoBClawShapes.control)
                .clickable(enabled = prefs.uiScale != 1.0f) { setScale(1.0f) }
                .padding(horizontal = 4.dp, vertical = 4.dp),
        )
    }
}

/** A square +/- stepper button: surfaceRaised fill + borderControl hairline, accent symbol. */
@Composable
private fun StepperButton(
    symbol: String,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val colors = LocalBoBClawColors
    Box(
        modifier = Modifier
            .size(34.dp)
            .clip(BoBClawShapes.control)
            .background(colors.surfaceRaised, BoBClawShapes.control)
            .border(1.dp, colors.borderControl, BoBClawShapes.control)
            .clickable(enabled = enabled) { onClick() },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = symbol,
            style = BoBClawType.title,
            color = if (enabled) colors.accent else colors.textMuted,
        )
    }
}

/** Number of swatches per row — hand-rolled wrapping (no experimental FlowRow). */
private const val ACCENT_SWATCHES_PER_ROW = 8

/**
 * The functional accent picker (lane 4b): a heading + a wrapping grid of all [ACCENT_PRESETS]
 * swatches. Tapping a swatch writes `prefs.accentName`; App re-derives the §3 token set and the
 * whole app re-skins live. The currently-selected swatch carries a ring (an outer bordered Box +
 * a small gap) so the choice reads at a glance.
 *
 * Wrapping is hand-rolled via `ACCENT_PRESETS.chunked(...)` → a `Column` of `Row`s to stay clear of
 * the experimental `FlowRow` API on Compose 1.6.11.
 */
@Composable
private fun AccentPickerRow(
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
) {
    val colors = LocalBoBClawColors
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(stringResource(Res.string.settings_accent_color), style = BoBClawType.title, color = colors.textPrimary)
        Spacer(Modifier.height(2.dp))
        Text(
            stringResource(Res.string.settings_accent_color_description),
            style = BoBClawType.body,
            color = colors.textSecondary,
        )
        Spacer(Modifier.height(12.dp))
        ACCENT_PRESETS.chunked(ACCENT_SWATCHES_PER_ROW).forEachIndexed { rowIndex, rowPresets ->
            if (rowIndex > 0) Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                rowPresets.forEach { preset ->
                    AccentSwatch(
                        color = preset.color,
                        selected = prefs.accentName == preset.name,
                        onClick = { onPrefsChange(prefs.copy(accentName = preset.name)) },
                    )
                }
            }
        }
    }
}

/**
 * One accent swatch: a fully-rounded circle filled with [color]. When [selected] it gets an outer
 * ring — a bordered box (in `textPrimary`) with a gap (transparent inner padding) so the ring reads
 * clearly against the fill. Unselected swatches carry a hairline `borderControl` outline so light
 * swatches stay visible against the card.
 */
@Composable
private fun AccentSwatch(
    color: Color,
    selected: Boolean,
    onClick: () -> Unit,
) {
    val colors = LocalBoBClawColors
    // Outer ring slot is a fixed 28dp box; the inner fill is inset so the selection ring sits
    // around it without resizing the swatch grid.
    Box(
        modifier = Modifier
            .size(28.dp)
            .clip(BoBClawShapes.full)
            .then(
                if (selected) {
                    Modifier.border(2.dp, colors.textPrimary, BoBClawShapes.full)
                } else {
                    Modifier
                }
            )
            .clickable { onClick() }
            .padding(if (selected) 4.dp else 0.dp),
        contentAlignment = Alignment.Center,
    ) {
        Box(
            modifier = Modifier
                .fillMaxSize()
                .clip(BoBClawShapes.full)
                .background(color, BoBClawShapes.full)
                .border(1.dp, colors.borderControl, BoBClawShapes.full),
        )
    }
}

/** A visually-present but disabled selector (DESIGN §5 theme/density stubs — not wired). */
@Composable
private fun StubSelectorRow(
    title: String,
    options: List<String>,
    selectedLabel: String,
) {
    val colors = LocalBoBClawColors
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(title, style = BoBClawType.title, color = colors.textMuted)
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            options.forEach { option ->
                val isSelected = option == selectedLabel
                Box(
                    modifier = Modifier
                        .clip(BoBClawShapes.pill)
                        .background(
                            if (isSelected) colors.surfaceAccent else colors.surfaceRaised,
                            BoBClawShapes.pill,
                        )
                        .border(
                            1.dp,
                            if (isSelected) colors.borderAccent else colors.borderControl,
                            BoBClawShapes.pill,
                        )
                        .padding(horizontal = 14.dp, vertical = 7.dp),
                ) {
                    Text(
                        text = option,
                        style = BoBClawType.label,
                        // muted across the board — the whole selector is disabled in 4a
                        color = colors.textMuted,
                    )
                }
            }
        }
    }
}

/** A simple "Not yet configured" detail pane for the non-functional sections. */
@Composable
private fun NotConfiguredPane(name: String) {
    val colors = LocalBoBClawColors
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                text = name,
                style = BoBClawType.title,
                color = colors.textPrimary,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = stringResource(Res.string.settings_not_yet_configured),
                style = BoBClawType.monoCaption,
                color = colors.textMuted,
                textAlign = TextAlign.Center,
            )
        }
    }
}

/** A titled card: a captioned (mono, muted) section header over a surfaceCard + borderCard panel. */
@Composable
private fun SettingsCard(
    caption: String,
    content: @Composable () -> Unit,
) {
    val colors = LocalBoBClawColors
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = caption,
            style = BoBClawType.monoCaption,
            color = colors.textMuted,
        )
        Spacer(Modifier.height(6.dp))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .clip(BoBClawShapes.card)
                .background(colors.surfaceCard, BoBClawShapes.card)
                .border(1.dp, colors.borderCard, BoBClawShapes.card)
                .padding(16.dp),
        ) {
            content()
        }
    }
}

/** Render a scale as `1.10×` — always 2 decimals, mono caller. Dep-free (no String.format on KMM). */
private fun formatScale(scale: Float): String {
    val hundredths = round(scale * 100f).toInt()          // e.g. 1.1f -> 110
    val whole = hundredths / 100
    val frac = hundredths % 100
    val fracStr = if (frac < 10) "0$frac" else "$frac"
    return "$whole.$fracStr×"                          // × = ×
}
