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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.bobclaw.model.CapabilityBackend
import com.bobclaw.network.RestClient
import com.bobclaw.network.UI_SCALE_MAX
import com.bobclaw.network.UI_SCALE_MIN
import com.bobclaw.network.UserPrefs
import kotlinx.datetime.Clock
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
    restClient: RestClient,
    onLogout: () -> Unit,
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
            // U10 (SPEC §7): every pane is now either live-bound or an honest read-only view — the
            // shared "Not yet configured" stub is GONE. Appearance is U9's (untouched here); Account /
            // Models / Connections / Approvals bind to real GET-only gateway calls; Advanced carries an
            // honest "later update" note (reserved for U11's voice/dev toggles — no invented surface).
            Box(modifier = Modifier.weight(1f).fillMaxHeight()) {
                when (pane) {
                    Pane.APPEARANCE -> AppearancePane(prefs = prefs, onPrefsChange = onPrefsChange)
                    Pane.ACCOUNT -> AccountPane(restClient = restClient, onLogout = onLogout)
                    Pane.MODELS -> ModelsPane(restClient = restClient)
                    Pane.CONNECTIONS -> ConnectionsPane(restClient = restClient)
                    Pane.APPROVALS -> ApprovalDefaultsPane(restClient = restClient)
                    Pane.ADVANCED -> AdvancedPane(prefs = prefs, onPrefsChange = onPrefsChange)
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
        SettingsCard(caption = stringResource(Res.string.settings_experience)) {
            // U9 (SPEC §6 / D6): the Simple/Pro experience knob (extends the U6 experienceLevel pref).
            // Simple (default) hides technical jargon — model names, backends, the routing table;
            // Pro exposes today's full surface. Writes prefs.experienceLevel; the app re-renders live
            // (chat chips + placeholder, Teams tabs, approvals literacy) with NO restart. Kept inside
            // AppearancePane so U10's other Settings panes (Models/Connections/Account) never collide.
            ExperienceLevelRow(prefs = prefs, onPrefsChange = onPrefsChange)
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_accent)) {
            // Lane 4b — the user-settable accent picker. Writes prefs.accentName; App re-derives the
            // whole color set from it and the entire app re-skins live (and the choice persists).
            AccentPickerRow(prefs = prefs, onPrefsChange = onPrefsChange)
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_theme)) {
            // Functional (A1): Dark/Light/System persisted in UserPrefs.theme; App re-derives the
            // §4.1 token set live for the resolved mode. Light ships beta-flagged.
            ThemeSelectorRow(prefs = prefs, onPrefsChange = onPrefsChange)
            Spacer(Modifier.height(12.dp))
            // Density stays a parked stub (DESIGN §5) — visually present, not wired.
            StubSelectorRow(
                title = stringResource(Res.string.settings_density_title),
                options = listOf(stringResource(Res.string.settings_comfortable), stringResource(Res.string.settings_compact)),
                selectedLabel = stringResource(Res.string.settings_comfortable),
            )
        }
    }
}

/**
 * The Simple/Pro experience selector (U9, SPEC §6 / D6). Two pills bound to `prefs.experienceLevel`
 * (the U6 pref, extended). Tapping one writes the value; the whole app re-renders live (chat surface,
 * Teams tabs, approvals literacy). Mirrors [ThemeSelectorRow]'s pill idiom.
 */
@Composable
private fun ExperienceLevelRow(
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
) {
    val colors = LocalBoBClawColors
    // (persisted value, label)
    val options = listOf(
        "simple" to stringResource(Res.string.settings_experience_simple),
        "pro" to stringResource(Res.string.settings_experience_pro),
    )
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(stringResource(Res.string.settings_experience_title), style = BoBClawType.title, color = colors.textPrimary)
        Spacer(Modifier.height(2.dp))
        Text(
            stringResource(Res.string.settings_experience_description),
            style = BoBClawType.body,
            color = colors.textSecondary,
        )
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            options.forEach { (value, label) ->
                val isSelected = prefs.experienceLevel == value
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
                        .clickable { if (!isSelected) onPrefsChange(prefs.copy(experienceLevel = value)) }
                        .padding(horizontal = 14.dp, vertical = 7.dp),
                ) {
                    Text(
                        text = label,
                        style = BoBClawType.label,
                        color = if (isSelected) colors.accent else colors.textBody,
                    )
                }
            }
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

/**
 * The functional theme selector (A1): Dark / Light / System pills. Tapping one writes
 * `prefs.theme`; App resolves it against the OS setting and re-derives the whole §4.1 token set,
 * re-skinning the app live (and the choice persists via [PrefCodec]). Light carries a `beta` tag
 * per the §6.3 pre-flight decision (ship Light beta-flagged first).
 */
@Composable
private fun ThemeSelectorRow(
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
) {
    val colors = LocalBoBClawColors
    // (persisted value, label, beta?)
    val options = listOf(
        Triple("dark", stringResource(Res.string.settings_dark), false),
        Triple("light", stringResource(Res.string.settings_light), true),
        Triple("system", stringResource(Res.string.settings_system), false),
    )
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(stringResource(Res.string.settings_theme_title), style = BoBClawType.title, color = colors.textPrimary)
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            options.forEach { (value, label, beta) ->
                val isSelected = prefs.theme == value
                Row(
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
                        .clickable { if (!isSelected) onPrefsChange(prefs.copy(theme = value)) }
                        .padding(horizontal = 14.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text = label,
                        style = BoBClawType.label,
                        color = if (isSelected) colors.accent else colors.textBody,
                    )
                    if (beta) {
                        Spacer(Modifier.width(6.dp))
                        BetaTag()
                    }
                }
            }
        }
    }
}

/** A tiny `beta` tag pill (warn-toned) — marks the Light theme as pre-release per §6.3. */
@Composable
private fun BetaTag() {
    val colors = LocalBoBClawColors
    Box(
        modifier = Modifier
            .clip(BoBClawShapes.pill)
            .background(colors.warn.copy(alpha = 0.18f), BoBClawShapes.pill)
            .padding(horizontal = 6.dp, vertical = 1.dp),
    ) {
        Text(
            text = stringResource(Res.string.settings_theme_beta),
            style = BoBClawType.monoCaption,
            color = colors.warn,
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

// ============================================================================================
// U10 (SPEC §7) — the real Settings panes. Each binds READ-ONLY to an existing gateway GET call
// (no new endpoints); a fetch failure degrades to an honest note, never a fake value. The pure,
// unit-tested logic (JWT identity/expiry, approval-defaults mapping) lives in SettingsPanels.kt.
// ============================================================================================

private const val EM_DASH = "—"

/**
 * ACCOUNT — identity + access-token expiry (decoded client-side from the current session JWT;
 * gateway sets `sub`=user_id + `exp`), and a logout that clears the session and returns to Login.
 * No `/me`-style endpoint exists, so identity comes from the token the client already holds.
 */
@Composable
private fun AccountPane(restClient: RestClient, onLogout: () -> Unit) {
    val colors = LocalBoBClawColors
    val accessToken = restClient.currentTokens()?.access
    val identity = jwtIdentity(accessToken)
    val expEpoch = jwtExpEpochSeconds(accessToken)
    val nowSeconds = Clock.System.now().epochSeconds
    val minsLeft = tokenExpiryMinutes(expEpoch, nowSeconds)

    Column(modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        SettingsCard(caption = stringResource(Res.string.settings_pane_account)) {
            KeyValueRow(
                label = stringResource(Res.string.settings_account_signed_in_as),
                value = identity ?: EM_DASH,
                mono = identity != null,
            )
            KeyValueRow(
                label = stringResource(Res.string.settings_account_token_expiry),
                value = when {
                    // No exp claim (or an unreadable token) ⇒ honest em-dash, never a fabricated "0m".
                    expEpoch == null || minsLeft == null -> EM_DASH
                    minsLeft <= 0L -> stringResource(Res.string.settings_account_token_expired)
                    else -> stringResource(
                        Res.string.settings_account_token_expires_in,
                        formatDurationShort(minsLeft),
                    )
                },
            )
            Spacer(Modifier.height(8.dp))
            MutedNote(stringResource(Res.string.settings_account_token_note))
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_account_session)) {
            // Logout clears the tokens (AuthManager.logout, wired through onLogout in App.kt) and
            // returns to the Login screen — same clear-then-navigate the chat menu logout does.
            Box(
                modifier = Modifier
                    .clip(BoBClawShapes.control)
                    .background(colors.alert.copy(alpha = 0.14f), BoBClawShapes.control)
                    .border(1.dp, colors.alert.copy(alpha = 0.5f), BoBClawShapes.control)
                    .clickable { onLogout() }
                    .padding(horizontal = 16.dp, vertical = 9.dp),
            ) {
                Text(stringResource(Res.string.chat_log_out), style = BoBClawType.label, color = colors.alert)
            }
            Spacer(Modifier.height(8.dp))
            MutedNote(stringResource(Res.string.settings_account_logout_help))
        }
    }
}

/**
 * MODELS & BACKENDS — the live `GET /capabilities` aggregate (the SAME registry the chat `/`
 * palette lists): a registry summary, the merged backend list (availability + model), and the faces.
 */
@Composable
private fun ModelsPane(restClient: RestClient) {
    val colors = LocalBoBClawColors
    val result = rememberLoad(restClient) { restClient.getCapabilities() }

    Column(modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        SettingsCard(caption = stringResource(Res.string.settings_pane_models)) {
            Text(
                stringResource(Res.string.settings_models_subtitle),
                style = BoBClawType.body,
                color = colors.textSecondary,
            )
            Spacer(Modifier.height(12.dp))
            LoadedContent(result) { caps ->
                KeyValueRow(
                    label = stringResource(Res.string.settings_models_registry),
                    value = stringResource(
                        Res.string.settings_models_counts,
                        caps.capabilities.faceCount.takeIf { it > 0 } ?: caps.faces.size,
                        caps.capabilities.backendCount.takeIf { it > 0 } ?: caps.backends.size,
                    ),
                )
                if (caps.warnings.isNotEmpty()) {
                    Spacer(Modifier.height(6.dp))
                    Text(
                        stringResource(Res.string.settings_models_degraded, caps.warnings.size),
                        style = BoBClawType.monoCaption,
                        color = colors.warn,
                    )
                }
            }
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_models_backends)) {
            LoadedContent(result) { caps ->
                if (caps.backends.isEmpty()) {
                    MutedNote(stringResource(Res.string.settings_models_none))
                } else {
                    caps.backends.forEachIndexed { i, b ->
                        if (i > 0) Spacer(Modifier.height(8.dp))
                        BackendRow(b)
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_models_faces)) {
            LoadedContent(result) { caps ->
                if (caps.faces.isEmpty()) {
                    MutedNote(stringResource(Res.string.settings_models_no_faces))
                } else {
                    caps.faces.forEachIndexed { i, f ->
                        if (i > 0) Spacer(Modifier.height(6.dp))
                        KeyValueRow(
                            label = f.displayName ?: f.name,
                            value = f.preferredBackend,
                            mono = true,
                        )
                    }
                }
            }
        }
    }
}

/** One merged backend: an availability dot, the backend name + its model, and an availability tag. */
@Composable
private fun BackendRow(b: CapabilityBackend) {
    val colors = LocalBoBClawColors
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = Modifier
                .size(8.dp)
                .clip(BoBClawShapes.full)
                .background(if (b.available) colors.success else colors.textMuted, BoBClawShapes.full),
        )
        Spacer(Modifier.width(10.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(b.backend, style = BoBClawType.label, color = colors.textBody)
            if (b.model != null) {
                Text(b.model, style = BoBClawType.monoCaption, color = colors.textMuted)
            }
        }
        Text(
            text = stringResource(
                if (b.available) Res.string.settings_models_available else Res.string.settings_models_unavailable,
            ),
            style = BoBClawType.monoCaption,
            color = if (b.available) colors.success else colors.textMuted,
        )
    }
}

/**
 * CONNECTIONS — the configured gateway base URL (from the RestClient itself) + live service health
 * from `GET /health` ({status, services:{name→url}}). Honest about the endpoint: it reports ONE
 * aggregate status plus a name→url service map (no per-service probe), so we show the aggregate once.
 */
@Composable
private fun ConnectionsPane(restClient: RestClient) {
    val colors = LocalBoBClawColors
    val result = rememberLoad(restClient) { restClient.getHealth() }

    Column(modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        SettingsCard(caption = stringResource(Res.string.settings_pane_connections)) {
            KeyValueRow(
                label = stringResource(Res.string.settings_connections_gateway),
                value = restClient.gatewayBaseUrl(),
                mono = true,
            )
            LoadedContent(result) { rows ->
                val status = rows.firstOrNull()?.status
                val ok = status != null && (status.equals("ok", true) || status.equals("healthy", true))
                Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                    Text(
                        stringResource(Res.string.settings_connections_status),
                        style = BoBClawType.label,
                        color = colors.textMuted,
                        modifier = Modifier.width(140.dp),
                    )
                    Spacer(Modifier.width(12.dp))
                    Text(
                        text = status ?: EM_DASH,
                        style = BoBClawType.monoLabel,
                        color = if (ok) colors.success else colors.warn,
                        modifier = Modifier.weight(1f),
                    )
                }
            }
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_connections_services)) {
            LoadedContent(result) { rows ->
                if (rows.isEmpty()) {
                    MutedNote(stringResource(Res.string.settings_connections_no_services))
                } else {
                    rows.forEachIndexed { i, h ->
                        if (i > 0) Spacer(Modifier.height(6.dp))
                        KeyValueRow(label = h.name, value = h.message ?: EM_DASH, mono = true)
                    }
                }
            }
        }
    }
}

/**
 * APPROVAL DEFAULTS — a READ-ONLY view of "current defaults v1" sourced from `GET /approvals/kinds`
 * (which kinds require a human, which can only ever be proposed). Editing is deferred — stated on the
 * page. The mapping/sort is the unit-tested [approvalDefaultRows].
 */
@Composable
private fun ApprovalDefaultsPane(restClient: RestClient) {
    val colors = LocalBoBClawColors
    val result = rememberLoad(restClient) { restClient.getApprovalKinds() }

    Column(modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        SettingsCard(caption = stringResource(Res.string.settings_pane_approvals)) {
            Text(
                stringResource(Res.string.settings_approvals_readonly_note),
                style = BoBClawType.body,
                color = colors.textSecondary,
            )
            Spacer(Modifier.height(12.dp))
            LoadedContent(result) { kinds ->
                val rows = approvalDefaultRows(kinds)
                if (rows.isEmpty()) {
                    MutedNote(stringResource(Res.string.settings_approvals_none))
                } else {
                    rows.forEachIndexed { i, r ->
                        if (i > 0) Spacer(Modifier.height(12.dp))
                        ApprovalDefaultRowView(r)
                    }
                }
            }
        }
    }
}

@Composable
private fun ApprovalDefaultRowView(r: ApprovalDefaultRow) {
    val colors = LocalBoBClawColors
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text(r.label, style = BoBClawType.label, color = colors.textBody, modifier = Modifier.weight(1f))
            if (r.proposalOnly) {
                SettingsBadge(stringResource(Res.string.settings_approvals_proposal_only), colors.accent)
                Spacer(Modifier.width(6.dp))
            }
            if (r.requiresHuman) {
                SettingsBadge(stringResource(Res.string.settings_approvals_requires_human), colors.warn)
            }
        }
        if (r.description.isNotBlank()) {
            Spacer(Modifier.height(2.dp))
            Text(r.description, style = BoBClawType.monoCaption, color = colors.textMuted)
        }
    }
}

/**
 * ADVANCED — the home for beta/preview flags (U10 reserved this pane for U11's voice toggle). Carries
 * the U11 `voice_beta` preview toggle (a real, live control) above an honest note that the remaining
 * developer options arrive later. Placed here so U9's Appearance work and U10's data panes are untouched.
 */
@Composable
private fun AdvancedPane(prefs: UserPrefs, onPrefsChange: (UserPrefs) -> Unit) {
    val colors = LocalBoBClawColors
    Column(modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        SettingsCard(caption = stringResource(Res.string.settings_voice_caption)) {
            // U11 (SPEC §7): the voice_beta preview flag. ON reveals the inert mic + read-aloud
            // affordances across the chat surface; no speech engine is wired yet (a preview).
            VoiceBetaRow(prefs = prefs, onPrefsChange = onPrefsChange)
        }
        Spacer(Modifier.height(12.dp))
        SettingsCard(caption = stringResource(Res.string.settings_pane_advanced)) {
            Text(
                stringResource(Res.string.settings_advanced_note),
                style = BoBClawType.body,
                color = colors.textSecondary,
            )
        }
    }
}

/**
 * The `voice_beta` preview toggle (U11, SPEC §7). Two pills bound to `prefs.voiceBeta`, mirroring
 * [ExperienceLevelRow]/[ThemeSelectorRow]. A `beta` tag marks it pre-release. Flipping it ON reveals
 * the inert voice affordances (disabled mic + "coming soon" tooltip, read-aloud placeholder) live —
 * no restart. Nothing else changes when OFF (the default): the UI is byte-identical to today.
 */
@Composable
private fun VoiceBetaRow(prefs: UserPrefs, onPrefsChange: (UserPrefs) -> Unit) {
    val colors = LocalBoBClawColors
    // (persisted value, label)
    val options = listOf(
        false to stringResource(Res.string.settings_voice_off),
        true to stringResource(Res.string.settings_voice_on),
    )
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(Res.string.settings_voice_title), style = BoBClawType.title, color = colors.textPrimary)
            Spacer(Modifier.width(8.dp))
            BetaTag()
        }
        Spacer(Modifier.height(2.dp))
        Text(
            stringResource(Res.string.settings_voice_description),
            style = BoBClawType.body,
            color = colors.textSecondary,
        )
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            options.forEach { (value, label) ->
                val isSelected = prefs.voiceBeta == value
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
                        .clickable { if (!isSelected) onPrefsChange(prefs.copy(voiceBeta = value)) }
                        .padding(horizontal = 14.dp, vertical = 7.dp),
                ) {
                    Text(
                        text = label,
                        style = BoBClawType.label,
                        color = if (isSelected) colors.accent else colors.textBody,
                    )
                }
            }
        }
    }
}

// ---- shared U10 pane primitives -------------------------------------------------------------

/** Fetch [fetch] once (keyed on [key]); returns null while loading, then a success/failure Result. */
@Composable
private fun <T> rememberLoad(key: Any?, fetch: suspend () -> T): Result<T>? {
    var state by remember(key) { mutableStateOf<Result<T>?>(null) }
    LaunchedEffect(key) { state = runCatching { fetch() } }
    return state
}

/** Render [result]: a muted "Loading…" while null, an honest warn note on failure, else [content]. */
@Composable
private fun <T> LoadedContent(result: Result<T>?, content: @Composable (T) -> Unit) {
    val colors = LocalBoBClawColors
    when {
        result == null -> MutedNote(stringResource(Res.string.settings_loading))
        result.isFailure -> Text(
            stringResource(Res.string.settings_load_error),
            style = BoBClawType.body,
            color = colors.warn,
        )
        else -> content(result.getOrThrow())
    }
}

/** A muted label → value line; [mono] renders the value in the machine (mono) face. */
@Composable
private fun KeyValueRow(label: String, value: String, mono: Boolean = false) {
    val colors = LocalBoBClawColors
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Text(label, style = BoBClawType.label, color = colors.textMuted, modifier = Modifier.width(140.dp))
        Spacer(Modifier.width(12.dp))
        Text(
            value,
            style = if (mono) BoBClawType.monoLabel else BoBClawType.body,
            color = colors.textBody,
            modifier = Modifier.weight(1f),
        )
    }
}

/** A small muted mono caption note (loading / degraded / helper copy). */
@Composable
private fun MutedNote(text: String) {
    Text(text, style = BoBClawType.monoCaption, color = LocalBoBClawColors.textMuted)
}

/** A tiny tinted pill badge (proposal-only / requires-you), tone-colored. */
@Composable
private fun SettingsBadge(text: String, tone: Color) {
    Box(
        modifier = Modifier
            .clip(BoBClawShapes.pill)
            .background(tone.copy(alpha = 0.16f), BoBClawShapes.pill)
            .padding(horizontal = 8.dp, vertical = 2.dp),
    ) {
        Text(text, style = BoBClawType.monoCaption, color = tone)
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
