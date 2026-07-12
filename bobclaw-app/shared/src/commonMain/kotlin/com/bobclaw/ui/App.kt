package com.bobclaw.ui

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.key
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.auth.AuthManager
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.Config
import com.bobclaw.network.NoopPrefStore
import com.bobclaw.network.NoopSessionStore
import com.bobclaw.network.PrefStore
import com.bobclaw.network.RestClient
import com.bobclaw.network.SessionStore
import com.bobclaw.network.UserPrefs
import com.bobclaw.model.Capabilities
import com.bobclaw.ui.components.AskBobBubble
import com.bobclaw.ui.components.AskBobPlacement
import com.bobclaw.ui.components.askBobPlacement
import com.bobclaw.ui.components.NavRail
import com.bobclaw.ui.screens.ApprovalsScreen
import com.bobclaw.ui.screens.ChatScreen
import com.bobclaw.ui.screens.CouncilScreen
import com.bobclaw.ui.screens.LoginScreen
import com.bobclaw.ui.screens.MemoryGraphRenderer
import com.bobclaw.ui.screens.MemoryScreen
import com.bobclaw.ui.screens.PlaceholderGraphRenderer
import com.bobclaw.ui.screens.PlaceholderScreen
import com.bobclaw.ui.screens.TeamsScreen
import com.bobclaw.ui.screens.SettingsScreen
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColorSet
import com.bobclaw.ui.theme.LocalBoBClawColors
import com.bobclaw.ui.theme.LocalBoBClawFonts
import com.bobclaw.ui.theme.rememberBobclawFonts
import com.bobclaw.ui.theme.accentColorFor
import com.bobclaw.ui.theme.bobclawColors
import com.bobclaw.ui.theme.resolveThemeMode
import com.bobclaw.ui.tiles.ApprovalsTile
import com.bobclaw.ui.tiles.kpi.ActiveConversationsTile
import com.bobclaw.ui.tiles.BackendHealthTile
import com.bobclaw.ui.tiles.ConversationListTile
import com.bobclaw.ui.tiles.IdeaInboxTile
import com.bobclaw.ui.tiles.ScheduledFiresTile
import kotlinx.coroutines.delay

/**
 * The real platform/system [Density] captured at [App], BEFORE the `uiScale` override on
 * `LocalDensity`. Compose Desktop's `SwingPanel` sizes its embedded heavyweight AWT component
 * against `LocalDensity` but paints it at the system scale, so a custom density (uiScale != 1.0)
 * shrinks interop surfaces to `1/uiScale` of their Compose slot (black gutters on the Memory-3D
 * canvas + chat artifact pane). Interop hosts re-provide THIS density around their `SwingPanel`
 * so the surface fills its box at any display scale. Null until [App] provides it.
 */
val LocalInteropDensity = staticCompositionLocalOf<Density?> { null }

/**
 * Top-level navigation state (no Voyager — a simple hoisted state machine for the MVP).
 *
 * `RESTORING` and `LOGGED_OUT` are full-screen (no rail). Once logged in we render the
 * persistent left nav rail + a content host (DESIGN §4); the active surface is tracked by
 * [RailDest] instead of dedicated `Screen` states.
 */
private enum class Screen { RESTORING, LOGGED_OUT, LOGGED_IN }

/**
 * Rail destinations (MS9 U1 / SPEC §2 D1): **Home · Chat · Council · Teams · Memory · Approvals**.
 * Home (was Dashboard) is the landing surface; Memory wires to the placeholder (U4b fills it).
 * Routing's top-level destination is retired — its table now lives in a Teams tab (see TeamsScreen).
 * Council / Memory / Approvals remain placeholders until their own U-lane sprints land.
 */
enum class RailDest { HOME, CHAT, COUNCIL, TEAMS, MEMORY, APPROVALS }

@Composable
fun App(
    sessionStore: SessionStore = NoopSessionStore,
    prefStore: PrefStore = NoopPrefStore,
    artifactRenderer: @Composable (html: String?, url: String?, modifier: Modifier) -> Unit = { _, _, _ -> },
    // The Memory 3D graph canvas (U4b) — injected like artifactRenderer so commonMain stays
    // JCEF-free. Desktop supplies the embedded-Chromium renderer; other targets fall back.
    memoryGraphRenderer: MemoryGraphRenderer = { g, o, s, m -> PlaceholderGraphRenderer(g, o, s, m) },
    applyPlatformLocale: (String) -> Unit = {},
) {
    // ONE RestClient instance, shared by AuthManager (it stores tokens into it) and all REST calls.
    val restClient = remember { RestClient(Config.BASE_URL) }
    val authManager = remember { AuthManager(restClient, sessionStore) }
    val webSocket = remember { BoBClawWebSocket(Config.WS_URL) }

    // User prefs as reactive root state. Loaded once on launch; on change we update state AND
    // persist (lane 4a: only uiScale is functional — it drives the root LocalDensity override below).
    var prefs by remember { mutableStateOf(prefStore.load()) }
    val onPrefsChange: (UserPrefs) -> Unit = { p ->
        prefs = p
        prefStore.save(p)
    }

    // Try to restore a persisted session on launch (silent refresh); skip login if it works.
    var screen by remember { mutableStateOf(Screen.RESTORING) }
    // Active surface inside the logged-in rail shell. Default to Home (SPEC §2: the landing surface).
    var railDest by remember { mutableStateOf(RailDest.HOME) }
    // Pending conversation id requested from the dashboard; consumed by ChatScreen on enter.
    var pendingConversationId by remember { mutableStateOf<String?>(null) }
    // Settings now has a real screen (lane 4a) — the rail's gear toggles it.
    var showSettings by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        screen = if (authManager.tryRestore()) Screen.LOGGED_IN else Screen.LOGGED_OUT
    }

    // Apply the UI-scale pref app-wide: override LocalDensity at the root so EVERY screen (incl.
    // the login/restoring screens) rescales. Multiply the platform density; keep the user's fontScale.
    // ALSO provide the accent-driven color set (lane 4b): re-deriving the whole §3 token set from
    // prefs.accentName here re-skins the entire app live whenever the Settings swatch picker writes
    // a new accentName (prefs is reactive root state). Both overrides live on the SAME provider.
    val baseDensity = LocalDensity.current
    // Resolve the persisted theme pref (dark|light|system) against the OS setting into a concrete
    // mode, then derive the whole §4.1 token set for that mode + accent. `system` follows the OS
    // live (isSystemInDarkTheme recomposes on OS change); Light ships beta-flagged (Settings toggle).
    val themeMode = resolveThemeMode(prefs.theme, isSystemInDarkTheme())
    // Restart-free locale (i18n): apply the platform locale, then re-key the tree so Compose
    // Resources re-resolve to values-zh-rCN / values-zh-rTW with NO app restart.
    remember(prefs.locale) { applyPlatformLocale(prefs.locale); prefs.locale }
    key(prefs.locale) {
    CompositionLocalProvider(
        LocalBoBClawColorSet provides bobclawColors(accentColorFor(prefs.accentName), themeMode),
        LocalBoBClawFonts provides rememberBobclawFonts(),
        LocalDensity provides Density(baseDensity.density * prefs.uiScale, baseDensity.fontScale),
        // Expose the un-scaled system density so heavyweight interop (SwingPanel/JCEF) can undo
        // the uiScale multiplier and fill its slot — see [LocalInteropDensity].
        LocalInteropDensity provides baseDensity,
    ) {
        when (screen) {
            Screen.RESTORING -> RestoringScreen()
            Screen.LOGGED_OUT -> LoginScreen(
                authManager = authManager,
                onLoggedIn = {
                    railDest = RailDest.HOME
                    showSettings = false
                    screen = Screen.LOGGED_IN
                },
            )
            Screen.LOGGED_IN -> LoggedInShell(
                authManager = authManager,
                restClient = restClient,
                webSocket = webSocket,
                railDest = railDest,
                showSettings = showSettings,
                prefs = prefs,
                onPrefsChange = onPrefsChange,
                onSelectDest = { dest ->
                    showSettings = false
                    // Selecting Chat directly from the rail is a "plain" entry — drop any stale
                    // pending id from a prior dashboard open (matches the old back-to-chat behavior).
                    // The dashboard's onOpenConversation path sets a fresh id AFTER this.
                    if (dest == RailDest.CHAT) pendingConversationId = null
                    railDest = dest
                },
                onSettings = { showSettings = true },
                onLogout = { screen = Screen.LOGGED_OUT },
                pendingConversationId = pendingConversationId,
                onOpenConversation = { convId ->
                    pendingConversationId = convId
                    showSettings = false
                    railDest = RailDest.CHAT
                },
                artifactRenderer = artifactRenderer,
                memoryGraphRenderer = memoryGraphRenderer,
            )
        }
    }
    }
}

/**
 * The logged-in shell: persistent [NavRail] on the left + a content host on the right
 * (DESIGN §4). Chat and Dashboard wrap the existing composables unchanged; everything
 * else is a [PlaceholderScreen].
 */
@Composable
private fun LoggedInShell(
    authManager: AuthManager,
    restClient: RestClient,
    webSocket: BoBClawWebSocket,
    railDest: RailDest,
    showSettings: Boolean,
    prefs: UserPrefs,
    onPrefsChange: (UserPrefs) -> Unit,
    onSelectDest: (RailDest) -> Unit,
    onSettings: () -> Unit,
    onLogout: () -> Unit,
    pendingConversationId: String?,
    onOpenConversation: (String) -> Unit,
    artifactRenderer: @Composable (html: String?, url: String?, modifier: Modifier) -> Unit,
    memoryGraphRenderer: MemoryGraphRenderer,
) {
    // Live approvals badge — reuse the same poll ApprovalsTile uses (restClient.getApprovals(),
    // pending-filtered, every 10s). Fail-open to 0 on error so a flaky gateway never blanks the rail.
    var approvalsCount by remember { mutableStateOf(0) }
    LaunchedEffect(restClient) {
        while (true) {
            try {
                approvalsCount = restClient.getApprovals().count { it.status == "pending" }
            } catch (_: Exception) {
                // leave the last good count; a transient error shouldn't flap the badge
            }
            delay(10_000)
        }
    }

    // U5: the live action registry (GET /capabilities) the Ask-Bob bubble filters by page_scope.
    // Fetched once; null until it loads (bubble then offers Guide mode only). Fail-open to null.
    var capabilities by remember { mutableStateOf<Capabilities?>(null) }
    LaunchedEffect(restClient) {
        capabilities = runCatching { restClient.getCapabilities() }.getOrNull()
    }
    // U5: the helper bubble rides the SAME chat WS. Connect it here (idempotent — ChatScreen also
    // connects) so the bubble can talk to Bob from a non-chat page where ChatScreen isn't mounted.
    LaunchedEffect(Unit) {
        authManager.getAccessToken()?.let { webSocket.connect(it) }
    }

    Row(modifier = Modifier.fillMaxSize().background(LocalBoBClawColors.canvas)) {
        NavRail(
            selected = railDest,
            onSelect = onSelectDest,
            onLogout = onLogout,
            onSettings = onSettings,
            approvalsCount = approvalsCount,
        )
        Box(modifier = Modifier.weight(1f).fillMaxSize()) {
            // The current non-chat page's page_scope id for the Ask-Bob bubble (SPEC §3 / D3).
            // Chat is excluded (it IS the chat); Settings and every rail destination are helped.
            val bubblePage: String = when {
                showSettings -> "settings"
                railDest == RailDest.HOME -> "home"
                railDest == RailDest.TEAMS -> "teams"
                // Memory hosts the heavyweight JCEF 3D canvas, which paints ABOVE all Compose — a
                // floating bubble would be fully occluded. MS9-UD: Ask Bob is DOCKED there instead
                // (rendered INSIDE MemoryScreen as a shrinking right-side panel). askBobPlacement()
                // routes "memory" → DOCKED, so App.kt renders NO floating bubble on Memory.
                railDest == RailDest.MEMORY -> "memory"
                railDest == RailDest.APPROVALS -> "approvals"
                railDest == RailDest.COUNCIL -> "council"
                else -> "" // CHAT → no bubble
            }
            when {
                // Settings (DESIGN §5 / lane 4a) — functional UI-scale + parked stubs.
                showSettings -> SettingsScreen(
                    prefs = prefs,
                    onPrefsChange = onPrefsChange,
                    restClient = restClient,
                    // U10 Account pane logout: clear tokens + session, then return to Login —
                    // the same clear-then-navigate the chat menu logout uses.
                    onLogout = { authManager.logout(); onLogout() },
                )
                railDest == RailDest.CHAT -> ChatScreen(
                    authManager = authManager,
                    restClient = restClient,
                    webSocket = webSocket,
                    onLogout = onLogout,
                    locale = prefs.locale,
                    onSetLocale = { tag -> onPrefsChange(prefs.copy(locale = tag)) },
                    // ChatScreen's in-chat "dashboard" button switches the rail to Home (the surface
                    // formerly called Dashboard). ChatScreen's signature is unchanged (no chat edits).
                    onOpenDashboard = { onSelectDest(RailDest.HOME) },
                    openConversationId = pendingConversationId,
                    artifactRenderer = artifactRenderer,
                    // U9: Simple/Pro chat calibration rides the same experience_level pref.
                    experienceLevel = prefs.experienceLevel,
                    // U11: voice affordances (composer mic + per-message read-aloud) behind voice_beta.
                    voiceBeta = prefs.voiceBeta,
                )
                railDest == RailDest.HOME -> HomeScreen(
                    restClient = restClient,
                    onOpenConversation = onOpenConversation,
                )
                railDest == RailDest.COUNCIL -> CouncilScreen(
                    restClient = restClient,
                    webSocket = webSocket,
                )
                railDest == RailDest.TEAMS -> TeamsScreen(
                    restClient = restClient,
                    experienceLevel = prefs.experienceLevel,
                    // MS9-W6 (Ask-Bob-on-Teams): the SAME U3 action registry + D12 confirm-once wiring the
                    // floating bubble uses, so applying a composed team (create/replace) is guardrailed,
                    // never a silent write, and shares the persisted create_team confirm.
                    capabilities = capabilities,
                    confirmedActions = prefs.confirmedActions,
                    onConfirmAction = { id ->
                        onPrefsChange(prefs.copy(confirmedActions = prefs.confirmedActions + id))
                    },
                    onOpenApprovals = { onSelectDest(RailDest.APPROVALS) },
                )
                railDest == RailDest.MEMORY -> MemoryScreen(
                    restClient = restClient,
                    graphRenderer = memoryGraphRenderer,
                    // MS9-UD: thread the Ask-Bob wiring so Memory renders a DOCKED bubble (same WS,
                    // same U3 action scope + D12 guardrails as the floating bubble elsewhere).
                    webSocket = webSocket,
                    capabilities = capabilities,
                    confirmedActions = prefs.confirmedActions,
                    onConfirmAction = { id ->
                        onPrefsChange(prefs.copy(confirmedActions = prefs.confirmedActions + id))
                    },
                    onOpenApprovals = { onSelectDest(RailDest.APPROVALS) },
                    // Server-side auto-routing picks the tool-capable face; presented as "Bob".
                    askBobFaceId = null,
                    voiceBeta = prefs.voiceBeta,
                )
                railDest == RailDest.APPROVALS -> ApprovalsScreen(
                    restClient = restClient,
                    webSocket = webSocket,
                    experienceLevel = prefs.experienceLevel,
                )
            }

            // U5 — the global Ask-Bob helper bubble, overlaid on every non-chat page (D3).
            // MS9-UD: only FLOATING pages get the overlaid bubble here; a DOCKED page (Memory)
            // renders its own dock inside the screen (above), and null (Chat) gets none.
            if (askBobPlacement(bubblePage) == AskBobPlacement.FLOATING) {
                AskBobBubble(
                    page = bubblePage,
                    // A lightweight visible-state snapshot. Richer per-screen snapshots are a
                    // follow-up; this already gives Bob the page + a live figure to reason over.
                    pageSnapshot = { "Screen: $bubblePage. Pending approvals: $approvalsCount." },
                    webSocket = webSocket,
                    restClient = restClient,
                    capabilities = capabilities,
                    // Server-side auto-routing picks the tool-capable face; presented as "Bob".
                    faceId = null,
                    confirmedActions = prefs.confirmedActions,
                    onConfirmAction = { id ->
                        onPrefsChange(prefs.copy(confirmedActions = prefs.confirmedActions + id))
                    },
                    onOpenApprovals = { onSelectDest(RailDest.APPROVALS) },
                    // U11: inert mic in the bubble's Guide-mode input row, behind voice_beta.
                    voiceBeta = prefs.voiceBeta,
                )
            }
        }
    }
}

@Composable
private fun RestoringScreen() {
    GradientBackground {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                CircularProgressIndicator(color = BoBClawColors.AccentGreen)
                Spacer(Modifier.height(12.dp))
                Text(stringResource(Res.string.app_restoring_session), color = BoBClawColors.TextSecondary)
            }
        }
    }
}

/**
 * Home (SPEC §2 / D2), the landing surface (was "Dashboard"). Real-data-or-delete: EVERY tile
 * binds to a live gateway endpoint. The mock tiles (Tests Passing, Spend/Tokens Today, Build
 * Sessions, Workers In Flight — no live route today) and the static "insight" strip (System
 * Health / Recent Events / Alert Summary — hardcoded) are DELETED. Survivors + their endpoints:
 *   · Active Conversations KPI + Conversations list → GET /conversations
 *   · Backend Health                                → GET /health
 *   · Scheduled fires (NEW)                          → GET /profiles (schedule.cron)
 *   · Idea Inbox                                     → GET /ideas
 *   · Approvals (KPI + full list)                    → GET /approvals
 */
@Composable
private fun HomeScreen(
    restClient: RestClient,
    onOpenConversation: (String) -> Unit,
) {
    GradientBackground {
        BoxWithConstraints {
            val isCompact = maxWidth < 700.dp
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(16.dp)
                    .verticalScroll(rememberScrollState())
            ) {
                KpiStrip(restClient)
                Spacer(Modifier.height(12.dp))
                if (isCompact) {
                    CompactZoneSection(restClient, onOpenConversation)
                } else {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(0.dp)
                    ) {
                        WideZoneSection(restClient, onOpenConversation)
                    }
                }
                Spacer(Modifier.height(12.dp))
                ApprovalsSection(restClient)
                Spacer(Modifier.height(16.dp))
            }
        }
    }
}

/** Real-only KPI strip: a live conversation count (/conversations) + a live pending-approvals
 * count (/approvals). The former mock KPIs (build/tokens/spend/tests/workers) are deleted. */
@Composable
private fun KpiStrip(restClient: RestClient) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        ActiveConversationsTile(restClient = restClient, modifier = Modifier.weight(1f))
        ApprovalsTile(restClient = restClient, narrow = true, modifier = Modifier.weight(1f))
    }
}

@Composable
private fun RowScope.WideZoneSection(restClient: RestClient, onOpenConversation: (String) -> Unit) {
    ZoneColumn(title = stringResource(Res.string.app_coding), modifier = Modifier.weight(1f)) {
        ConversationListTile(restClient = restClient, onOpenConversation = onOpenConversation)
    }
    Spacer(Modifier.width(12.dp))
    ZoneColumn(title = stringResource(Res.string.app_orchestration), modifier = Modifier.weight(1f)) {
        BackendHealthTile(restClient = restClient)
    }
    Spacer(Modifier.width(12.dp))
    ZoneColumn(title = stringResource(Res.string.app_schedule), modifier = Modifier.weight(1f)) {
        ScheduledFiresTile(restClient = restClient)
    }
    Spacer(Modifier.width(12.dp))
    ZoneColumn(title = stringResource(Res.string.app_cognitive), modifier = Modifier.weight(1f)) {
        IdeaInboxTile(restClient = restClient)
    }
}

@Composable
private fun ColumnScope.CompactZoneSection(restClient: RestClient, onOpenConversation: (String) -> Unit) {
    ZoneColumn(title = stringResource(Res.string.app_coding), modifier = Modifier.fillMaxWidth()) {
        ConversationListTile(restClient = restClient, onOpenConversation = onOpenConversation)
    }
    Spacer(Modifier.height(12.dp))
    ZoneColumn(title = stringResource(Res.string.app_orchestration), modifier = Modifier.fillMaxWidth()) {
        BackendHealthTile(restClient = restClient)
    }
    Spacer(Modifier.height(12.dp))
    ZoneColumn(title = stringResource(Res.string.app_schedule), modifier = Modifier.fillMaxWidth()) {
        ScheduledFiresTile(restClient = restClient)
    }
    Spacer(Modifier.height(12.dp))
    ZoneColumn(title = stringResource(Res.string.app_cognitive), modifier = Modifier.fillMaxWidth()) {
        IdeaInboxTile(restClient = restClient)
    }
}

@Composable
private fun ApprovalsSection(restClient: RestClient) {
    ApprovalsTile(restClient = restClient, narrow = false, modifier = Modifier.fillMaxWidth())
}

@Composable
private fun ZoneColumn(
    title: String,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit
) {
    Column(modifier = modifier) {
        Text(
            text = title,
            color = BoBClawColors.TextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.Bold,
            letterSpacing = 1.sp
        )
        Spacer(Modifier.height(8.dp))
        content()
    }
}
