package com.bobclaw.ui

import androidx.compose.foundation.background
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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
import com.bobclaw.ui.components.NavRail
import com.bobclaw.ui.components.Tile
import com.bobclaw.ui.screens.ChatScreen
import com.bobclaw.ui.screens.LoginScreen
import com.bobclaw.ui.screens.PlaceholderScreen
import com.bobclaw.ui.screens.RoutingScreen
import com.bobclaw.ui.screens.TeamsScreen
import com.bobclaw.ui.screens.SettingsScreen
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColorSet
import com.bobclaw.ui.theme.LocalBoBClawColors
import com.bobclaw.ui.theme.accentColorFor
import com.bobclaw.ui.theme.bobclawColors
import com.bobclaw.ui.tiles.ApprovalsTile
import com.bobclaw.ui.tiles.kpi.ActiveConversationsTile
import com.bobclaw.ui.tiles.kpi.BuildSessionsTile
import com.bobclaw.ui.tiles.kpi.SpendTodayTile
import com.bobclaw.ui.tiles.kpi.TestsPassingTile
import com.bobclaw.ui.tiles.kpi.TokensTodayTile
import com.bobclaw.ui.tiles.kpi.WorkersInFlightTile
import com.bobclaw.ui.tiles.BackendHealthTile
import com.bobclaw.ui.tiles.ConversationListTile
import com.bobclaw.ui.tiles.IdeaInboxTile
import kotlinx.coroutines.delay

/**
 * Top-level navigation state (no Voyager — a simple hoisted state machine for the MVP).
 *
 * `RESTORING` and `LOGGED_OUT` are full-screen (no rail). Once logged in we render the
 * persistent left nav rail + a content host (DESIGN §4); the active surface is tracked by
 * [RailDest] instead of dedicated `Screen` states.
 */
private enum class Screen { RESTORING, LOGGED_OUT, LOGGED_IN }

/**
 * Rail destinations (DESIGN §4). Chat and Dashboard wrap the existing composables unchanged;
 * Council / Teams / Routing / Approvals are placeholders until their own lanes land.
 */
enum class RailDest { CHAT, COUNCIL, TEAMS, ROUTING, APPROVALS, DASHBOARD }

@Composable
fun App(
    sessionStore: SessionStore = NoopSessionStore,
    prefStore: PrefStore = NoopPrefStore,
    artifactRenderer: @Composable (html: String?, url: String?, modifier: Modifier) -> Unit = { _, _, _ -> },
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
    // Active surface inside the logged-in rail shell. Default to Chat (the daily-driver surface).
    var railDest by remember { mutableStateOf(RailDest.CHAT) }
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
    CompositionLocalProvider(
        LocalBoBClawColorSet provides bobclawColors(accentColorFor(prefs.accentName)),
        LocalDensity provides Density(baseDensity.density * prefs.uiScale, baseDensity.fontScale),
    ) {
        when (screen) {
            Screen.RESTORING -> RestoringScreen()
            Screen.LOGGED_OUT -> LoginScreen(
                authManager = authManager,
                onLoggedIn = {
                    railDest = RailDest.CHAT
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
            )
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

    Row(modifier = Modifier.fillMaxSize().background(LocalBoBClawColors.canvas)) {
        NavRail(
            selected = railDest,
            onSelect = onSelectDest,
            onLogout = onLogout,
            onSettings = onSettings,
            approvalsCount = approvalsCount,
        )
        Box(modifier = Modifier.weight(1f).fillMaxSize()) {
            when {
                // Settings (DESIGN §5 / lane 4a) — functional UI-scale + parked stubs.
                showSettings -> SettingsScreen(prefs = prefs, onPrefsChange = onPrefsChange)
                railDest == RailDest.CHAT -> ChatScreen(
                    authManager = authManager,
                    restClient = restClient,
                    webSocket = webSocket,
                    onLogout = onLogout,
                    // ChatScreen's in-chat dashboard button now just switches the rail destination
                    // (the rail also has Dashboard). ChatScreen's signature is unchanged.
                    onOpenDashboard = { onSelectDest(RailDest.DASHBOARD) },
                    openConversationId = pendingConversationId,
                    artifactRenderer = artifactRenderer,
                )
                railDest == RailDest.DASHBOARD -> Dashboard(
                    restClient = restClient,
                    onOpenConversation = onOpenConversation,
                )
                railDest == RailDest.COUNCIL -> PlaceholderScreen("Council")
                railDest == RailDest.TEAMS -> TeamsScreen(restClient = restClient)
                railDest == RailDest.ROUTING -> RoutingScreen(restClient = restClient)
                railDest == RailDest.APPROVALS -> PlaceholderScreen("Approvals")
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
                Text("Restoring session...", color = BoBClawColors.TextSecondary)
            }
        }
    }
}

/**
 * The original dashboard scaffold, now a rail destination. The in-content "< Back to chat"
 * button is gone — the rail owns navigation — but the tiles and the open-conversation
 * behavior are unchanged.
 */
@Composable
private fun Dashboard(
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
                Spacer(Modifier.height(12.dp))
                InsightStrip()
                Spacer(Modifier.height(16.dp))
            }
        }
    }
}

@Composable
private fun KpiStrip(restClient: RestClient) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        ActiveConversationsTile(modifier = Modifier.weight(1f))
        BuildSessionsTile(modifier = Modifier.weight(1f))
        TokensTodayTile(modifier = Modifier.weight(1f))
        SpendTodayTile(modifier = Modifier.weight(1f))
        TestsPassingTile(modifier = Modifier.weight(1f))
        WorkersInFlightTile(modifier = Modifier.weight(1f))
        ApprovalsTile(restClient = restClient, narrow = true, modifier = Modifier.weight(0.6f))
    }
}

@Composable
private fun RowScope.WideZoneSection(restClient: RestClient, onOpenConversation: (String) -> Unit) {
    ZoneColumn(title = "Coding", modifier = Modifier.weight(1f)) {
        ConversationListTile(restClient = restClient, onOpenConversation = onOpenConversation)
    }
    Spacer(Modifier.width(12.dp))
    ZoneColumn(title = "Orchestration", modifier = Modifier.weight(1f)) {
        BackendHealthTile(restClient = restClient)
    }
    Spacer(Modifier.width(12.dp))
    ZoneColumn(title = "Cognitive", modifier = Modifier.weight(1f)) {
        IdeaInboxTile(restClient = restClient)
    }
}

@Composable
private fun ColumnScope.CompactZoneSection(restClient: RestClient, onOpenConversation: (String) -> Unit) {
    ZoneColumn(title = "Coding", modifier = Modifier.fillMaxWidth()) {
        ConversationListTile(restClient = restClient, onOpenConversation = onOpenConversation)
    }
    Spacer(Modifier.height(12.dp))
    ZoneColumn(title = "Orchestration", modifier = Modifier.fillMaxWidth()) {
        BackendHealthTile(restClient = restClient)
    }
    Spacer(Modifier.height(12.dp))
    ZoneColumn(title = "Cognitive", modifier = Modifier.fillMaxWidth()) {
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

@Composable
private fun InsightStrip() {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Tile(title = "System Health", modifier = Modifier.weight(1f)) {
            Text("All systems nominal", color = BoBClawColors.KpiGreen)
        }
        Tile(title = "Recent Events", modifier = Modifier.weight(1f)) {
            Text("3 alerts in 24h", color = BoBClawColors.TextSecondary)
        }
        Tile(title = "Alert Summary", modifier = Modifier.weight(1f)) {
            Text("1 critical, 2 warning", color = BoBClawColors.TextSecondary)
        }
    }
}
