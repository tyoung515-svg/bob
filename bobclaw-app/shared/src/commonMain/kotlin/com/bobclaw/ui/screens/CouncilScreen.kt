package com.bobclaw.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.Conversation
import com.bobclaw.model.Team
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import kotlin.time.TimeSource
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val BlockedRed = Color(0xFFE74C3C)
private val ActiveAmber = Color(0xFFE0A83B)

private enum class CouncilStage { LAUNCH, LIVE, REPLAY }

/**
 * U8 — the Council deliberation THEATER (SPEC-UI-OVERHAUL §5 / D7), replacing the placeholder.
 * One screen, three states: **Launch** (pick a council/debate profile from `/profiles` + type an
 * ask → start the run), **Live** (seat cards, current round, who's-speaking, an Idea-ID convergence
 * board, a cost ticker + a converged/blocked banner — rendered from the U7 `council_event` frames by
 * [reduceCouncil]), and **Replay** (reconstruct a past run from a persisted COUNCIL HANDOFF blob via
 * [findCouncilRun]).
 *
 * Fence: READ + LAUNCH only — no profile editing here (that's Teams). All render logic lives in the
 * pure, unit-tested [CouncilTheater] module; this file is the Opus-authored Compose glue.
 *
 * Live-vs-mock: as of MS9-W1 the U7→U8 wiring is live — [startRun] sets `emitEvents=true` on the
 * start-turn, the gateway forwards it, core stamps `council_spec["emit_events"]`, and the SSE relay
 * streams the real `council_event`/`council_seat`/`council_synth` frames onto the shared chat WS,
 * where the [reduceCouncil] fold renders them. The **"Play mock run"** button remains as an explicit
 * demo fallback, driving the theater from a canned [mockCouncilRun] through the SAME reducer (the
 * mock-backend E2E §5 permits) when there is no live backend to launch against.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CouncilScreen(
    restClient: RestClient,
    webSocket: BoBClawWebSocket,
    modifier: Modifier = Modifier,
) {
    val colors = LocalBoBClawColors
    val scope = rememberCoroutineScope()

    var stage by remember { mutableStateOf(CouncilStage.LAUNCH) }
    var profiles by remember { mutableStateOf<List<Team>?>(null) }
    var selectedProfile by remember { mutableStateOf<String?>(null) }
    var ask by remember { mutableStateOf("") }
    var activeConvId by remember { mutableStateOf<String?>(null) }
    var theater by remember { mutableStateOf(CouncilTheaterState()) }
    var launchError by remember { mutableStateOf<String?>(null) }
    var live by remember { mutableStateOf(false) } // a real launch fired (vs a mock preview)

    // MS9-W5 (finding B, app belt-and-suspenders): stall detection. A monotonic clock tracks the
    // elapsed ms at the last folded frame (lastActivityMs) vs. a periodically-ticked now (nowMs);
    // councilStalled(...) turns the quiet gap into a "still working…" state so a wedged transport
    // never leaves the banner frozen on "Deliberating… $0.0000". The core fix already guarantees a
    // terminal frame on a real hang — this only trips if NO frame at all arrives.
    val startMark = remember { TimeSource.Monotonic.markNow() }
    var lastActivityMs by remember { mutableStateOf(0L) }
    var nowMs by remember { mutableStateOf(0L) }

    // Replay state.
    var conversations by remember { mutableStateOf<List<Conversation>?>(null) }
    var replay by remember { mutableStateOf<CouncilReplay?>(null) }
    var replayTitle by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(restClient) {
        profiles = runCatching { restClient.getProfiles() }.getOrDefault(emptyList())
    }

    // Live fold: a real run's frames arrive on the shared chat WS. MS9-W4 (fix A): the Ask-Bob helper
    // bubble shares this SAME WS, so its answer chunks used to fold into the council ANSWER. We now
    // run each frame through the pure [advanceCouncilFilter] FIRST (flight-bound + a live window keyed
    // on the council_* frames) and fold ONLY the frames that belong to THIS launched run. The filter
    // resets whenever activeConvId changes (each startRun opens a fresh council conversation).
    LaunchedEffect(activeConvId) {
        activeConvId ?: return@LaunchedEffect
        var filter = CouncilFilter()
        webSocket.incomingMessages.collect { msg ->
            val step = advanceCouncilFilter(filter, msg)
            filter = step.filter
            if (step.fold) {
                theater = reduceCouncil(theater, msg)
                lastActivityMs = startMark.elapsedNow().inWholeMilliseconds  // W5: mark activity
            }
        }
    }

    // W5: tick a monotonic clock while a live run is in flight so councilStalled(...) re-evaluates.
    LaunchedEffect(activeConvId) {
        activeConvId ?: return@LaunchedEffect
        while (true) {
            nowMs = startMark.elapsedNow().inWholeMilliseconds
            delay(2000)
        }
    }

    LaunchedEffect(stage) {
        if (stage == CouncilStage.REPLAY && conversations == null) {
            conversations = runCatching { restClient.getConversations(limit = 30, offset = 0) }.getOrDefault(emptyList())
        }
    }

    fun startRun(profile: String) {
        launchError = null
        scope.launch {
            try {
                val conv = restClient.createConversation(title = "Council: " + ask.take(40), faceId = null)
                theater = CouncilTheaterState()
                live = true
                activeConvId = conv.id
                webSocket.switchProfile(conv.id, profile)
                // MS9-W1: opt into the U7 council_event stream so the Live view renders the REAL
                // seat/round/convergence frames (not the mock). The gateway forwards emit_events
                // to core, which stamps council_spec["emit_events"] and relays the council frames.
                webSocket.sendMessage(conv.id, ask, faceId = null, emitEvents = true)
                stage = CouncilStage.LIVE
            } catch (e: Exception) {
                launchError = e.message ?: "Couldn't start the council run."
            }
        }
    }

    fun playMock() {
        live = false
        activeConvId = null
        stage = CouncilStage.LIVE
        scope.launch {
            theater = CouncilTheaterState()
            for (frame in mockCouncilRun()) {
                theater = reduceCouncil(theater, frame)
                delay(550)
            }
        }
    }

    GradientBackground(modifier = modifier) {
        Column(
            modifier = Modifier.fillMaxSize().padding(20.dp).verticalScroll(rememberScrollState())
        ) {
            Text("Council", color = colors.textPrimary, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(2.dp))
            Text(
                "Deliberation theater — launch a council, watch it work, or replay a past run.",
                color = colors.textSecondary, fontSize = 12.sp,
            )
            Spacer(Modifier.height(14.dp))

            // Stage tabs.
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                StageTab("Launch", stage == CouncilStage.LAUNCH) { stage = CouncilStage.LAUNCH }
                StageTab("Live", stage == CouncilStage.LIVE) { stage = CouncilStage.LIVE }
                StageTab("Replay", stage == CouncilStage.REPLAY) {
                    replay = null; replayTitle = null; stage = CouncilStage.REPLAY
                }
            }
            Spacer(Modifier.height(16.dp))

            when (stage) {
                CouncilStage.LAUNCH -> LaunchView(
                    profiles = profiles, selected = selectedProfile, ask = ask, error = launchError,
                    onSelect = { selectedProfile = it },
                    onAsk = { ask = it },
                    onStart = { selectedProfile?.let { startRun(it) } },
                    onDemo = { playMock() },
                )
                CouncilStage.LIVE -> LiveView(
                    theater = theater, live = live,
                    stalled = live && councilStalled(theater, nowMs - lastActivityMs),
                    onDemo = { playMock() },
                    onNewRun = { stage = CouncilStage.LAUNCH },
                )
                CouncilStage.REPLAY -> ReplayView(
                    conversations = conversations, replay = replay, title = replayTitle,
                    onPick = { conv ->
                        scope.launch {
                            replayTitle = conv.title ?: conv.id
                            val page = runCatching { restClient.getMessages(conv.id, limit = 50, before = null) }.getOrNull()
                            replay = page?.messages?.let { findCouncilRun(it) } ?: CouncilReplay(found = false)
                        }
                    },
                    onBack = { replay = null; replayTitle = null },
                )
            }
        }
    }
}

// ── Launch ──────────────────────────────────────────────────────────────────────────────────

@Composable
private fun LaunchView(
    profiles: List<Team>?,
    selected: String?,
    ask: String,
    error: String?,
    onSelect: (String) -> Unit,
    onAsk: (String) -> Unit,
    onStart: () -> Unit,
    onDemo: () -> Unit,
) {
    val colors = LocalBoBClawColors
    Text("PICK A PROFILE", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    Spacer(Modifier.height(8.dp))
    when {
        profiles == null -> Text("Loading profiles…", color = colors.textSecondary, fontSize = 12.sp)
        profiles.isEmpty() -> Text("No profiles available from the gateway.", color = colors.textSecondary, fontSize = 12.sp)
        else -> for (p in profiles) {
            val seatCount = p.roles.values.sumOf { it.size }
            val isSel = p.name == selected
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(bottom = 8.dp)
                    .clip(RoundedCornerShape(10.dp))
                    .background(if (isSel) colors.surfaceAccent else colors.surfaceCard)
                    .border(1.dp, if (isSel) colors.accent else Color.Transparent, RoundedCornerShape(10.dp))
                    .clickable { onSelect(p.name) }
                    .padding(12.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(p.name, color = colors.textPrimary, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(Modifier.weight(1f))
                    Text(p.shape ?: "team", color = colors.accent, fontSize = 11.sp)
                }
                Spacer(Modifier.height(2.dp))
                Text(
                    "$seatCount seat" + (if (seatCount == 1) "" else "s") +
                        (p.protocolBounds?.maxRounds?.let { " · up to $it rounds" } ?: "") +
                        (if (p.builtin) " · built-in" else ""),
                    color = colors.textMuted, fontSize = 11.sp,
                )
            }
        }
    }

    Spacer(Modifier.height(12.dp))
    Text("THE ASK", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    Spacer(Modifier.height(6.dp))
    BasicTextField(
        value = ask,
        onValueChange = onAsk,
        textStyle = TextStyle(color = colors.textBody, fontSize = 13.sp),
        cursorBrush = SolidColor(colors.accent),
        modifier = Modifier.fillMaxWidth(),
        decorationBox = { inner ->
            Box(
                Modifier.clip(RoundedCornerShape(8.dp)).background(colors.surfaceRaised).padding(12.dp)
            ) {
                if (ask.isEmpty()) {
                    Text("What should the council deliberate?", color = colors.textMuted, fontSize = 13.sp)
                }
                inner()
            }
        },
    )

    if (error != null) {
        Spacer(Modifier.height(8.dp))
        Text(error, color = BlockedRed, fontSize = 12.sp)
    }

    Spacer(Modifier.height(14.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        PrimaryButton("Start deliberation", enabled = selected != null && ask.isNotBlank(), onClick = onStart)
        GhostButton("Play mock run", onClick = onDemo)
    }
    Spacer(Modifier.height(6.dp))
    Text(
        "Start deliberation streams the real seat/round frames into the Live view. Play mock run " +
            "renders the theater from canned U7 frames — a demo fallback when there's no live backend.",
        color = colors.textMuted, fontSize = 10.sp,
    )
}

// ── Live ────────────────────────────────────────────────────────────────────────────────────

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun LiveView(
    theater: CouncilTheaterState,
    live: Boolean,
    stalled: Boolean = false,
    onDemo: () -> Unit,
    onNewRun: () -> Unit,
) {
    val colors = LocalBoBClawColors

    // Banner. W5: while RUNNING, a stalled run reads "Still working…" (in amber) rather than a
    // frozen "Deliberating…" — the honest signal that no terminal frame has arrived yet.
    val (bannerColor, bannerText) = when (theater.banner) {
        TheaterBanner.RUNNING ->
            if (stalled) ActiveAmber to "Still working…" else colors.accent to "Deliberating…"
        TheaterBanner.CONVERGED -> colors.success to "Converged"
        TheaterBanner.BLOCKED -> BlockedRed to "Blocked"
    }
    Box(
        modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
            .background(bannerColor.copy(alpha = 0.15f)).padding(14.dp)
    ) {
        Column {
            Text(bannerText, color = bannerColor, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            val sub = buildList {
                add("Round ${theater.round}")
                theater.mode?.let { add(it) }
                add(formatUsd(theater.costUsd))
                theater.reason?.let { add(it) }
            }.joinToString("  ·  ")
            Text(sub, color = colors.textSecondary, fontSize = 11.sp)
            if (stalled) {
                Text(
                    "No terminal frame yet — the run may be slow or the service wedged." +
                        (theater.seatTokens.takeIf { it > 0 }?.let { " ~$it tok in so far." } ?: ""),
                    color = colors.textMuted, fontSize = 10.sp,
                )
            }
            if (!live) {
                Text("mock run (canned frames)", color = colors.textMuted, fontSize = 10.sp)
            }
        }
    }

    // Who's speaking.
    val speaker = theater.currentSpeaker?.let { s -> theater.seats.firstOrNull { it.seat == s } }
    if (speaker != null) {
        Spacer(Modifier.height(10.dp))
        Text(
            "🗣  ${speaker.posture}${speaker.backend?.let { " · $it" } ?: ""} is speaking…",
            color = colors.accent, fontSize = 12.sp,
        )
    }

    // Seat cards.
    if (theater.seats.isNotEmpty()) {
        Spacer(Modifier.height(12.dp))
        Text("SEATS", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(6.dp))
        for (seat in theater.seats) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(bottom = 6.dp)
                    .clip(RoundedCornerShape(8.dp)).background(colors.surfaceCard)
                    .border(
                        1.dp,
                        if (seat.speaking) colors.accent else Color.Transparent,
                        RoundedCornerShape(8.dp),
                    )
                    .padding(10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(seat.posture, color = colors.textPrimary, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                seat.backend?.let {
                    Spacer(Modifier.width(8.dp))
                    Text(it, color = colors.textMuted, fontSize = 10.sp, fontFamily = FontFamily.Monospace)
                }
                Spacer(Modifier.weight(1f))
                val (statusText, statusColor) = when {
                    seat.speaking -> "speaking" to colors.accent
                    seat.status == "ok" -> "done" to colors.success
                    seat.status != null -> (seat.status ?: "") to BlockedRed
                    else -> "waiting" to colors.textMuted
                }
                seat.tokens?.let {
                    Text("$it tok", color = colors.textMuted, fontSize = 10.sp)
                    Spacer(Modifier.width(8.dp))
                }
                Text(statusText, color = statusColor, fontSize = 11.sp)
            }
        }
    }

    // Convergence board.
    if (theater.convergence.isNotEmpty()) {
        Spacer(Modifier.height(12.dp))
        Text("CONVERGENCE BOARD", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(6.dp))
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            for ((id, status) in theater.convergence) {
                val c = if (status == IdeaStatus.RESOLVED) colors.success else ActiveAmber
                Box(
                    Modifier.clip(RoundedCornerShape(6.dp)).background(c.copy(alpha = 0.16f))
                        .padding(horizontal = 8.dp, vertical = 3.dp)
                ) {
                    Text(
                        "$id · ${if (status == IdeaStatus.RESOLVED) "resolved" else "active"}",
                        color = c, fontSize = 11.sp,
                    )
                }
            }
        }
    }

    // Answer.
    if (theater.answer.isNotBlank()) {
        Spacer(Modifier.height(12.dp))
        Text("ANSWER", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(4.dp))
        Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(colors.surfaceRaised).padding(12.dp)) {
            Text(theater.answer, color = colors.textBody, fontSize = 12.sp, maxLines = 24, overflow = TextOverflow.Ellipsis)
        }
    }

    Spacer(Modifier.height(16.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        GhostButton("Play mock run", onClick = onDemo)
        GhostButton("New run", onClick = onNewRun)
    }
}

// ── Replay ──────────────────────────────────────────────────────────────────────────────────

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ReplayView(
    conversations: List<Conversation>?,
    replay: CouncilReplay?,
    title: String?,
    onPick: (Conversation) -> Unit,
    onBack: () -> Unit,
) {
    val colors = LocalBoBClawColors
    if (replay == null) {
        Text("PICK A PAST CONVERSATION", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))
        when {
            conversations == null -> Text("Loading conversations…", color = colors.textSecondary, fontSize = 12.sp)
            conversations.isEmpty() -> Text("No conversations found.", color = colors.textSecondary, fontSize = 12.sp)
            else -> for (conv in conversations) {
                Column(
                    modifier = Modifier.fillMaxWidth().padding(bottom = 6.dp)
                        .clip(RoundedCornerShape(8.dp)).background(colors.surfaceCard)
                        .clickable { onPick(conv) }.padding(10.dp),
                ) {
                    Text(conv.title ?: conv.id, color = colors.textPrimary, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    conv.lastMessagePreview?.let {
                        Text(it, color = colors.textMuted, fontSize = 10.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
        return
    }

    // Back + title.
    Row(verticalAlignment = Alignment.CenterVertically) {
        GhostButton("← Back", onClick = onBack)
        Spacer(Modifier.width(10.dp))
        Text(title ?: "Replay", color = colors.textPrimary, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
    Spacer(Modifier.height(12.dp))

    if (!replay.found) {
        Text("No council run found in this conversation.", color = colors.textSecondary, fontSize = 12.sp)
        return
    }

    val (bannerColor, bannerText) = when (replay.outcome) {
        TheaterBanner.CONVERGED -> colors.success to "Converged"
        TheaterBanner.BLOCKED -> BlockedRed to "Blocked"
        TheaterBanner.RUNNING -> ActiveAmber to "Unresolved (active debate persisted)"
    }
    Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(bannerColor.copy(alpha = 0.15f)).padding(14.dp)) {
        Column {
            Text(bannerText, color = bannerColor, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            if (replay.ranVoices != null && replay.totalVoices != null) {
                Text(
                    "ran with ${replay.ranVoices} of ${replay.totalVoices} voices" +
                        (if (replay.unavailable.isNotEmpty()) " · unavailable: ${replay.unavailable.joinToString(", ")}" else ""),
                    color = colors.textSecondary, fontSize = 11.sp,
                )
            }
        }
    }

    IdeaBoard("RESOLVED", replay.resolved, colors.success)
    IdeaBoard("ACTIVE DEBATE", replay.activeDebate, ActiveAmber)
    IdeaBoard("BLOCKED", replay.blocked, BlockedRed)
    if (replay.nextTask.isNotBlank()) {
        Spacer(Modifier.height(10.dp))
        Text("NEXT TASK", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        Text(replay.nextTask, color = colors.textBody, fontSize = 12.sp)
    }
    if (replay.body.isNotBlank()) {
        Spacer(Modifier.height(10.dp))
        Text("SYNTHESIS", color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(4.dp))
        Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(colors.surfaceRaised).padding(12.dp)) {
            Text(replay.body, color = colors.textBody, fontSize = 12.sp, maxLines = 24, overflow = TextOverflow.Ellipsis)
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun IdeaBoard(label: String, ids: List<String>, color: Color) {
    if (ids.isEmpty()) return
    val colors = LocalBoBClawColors
    Spacer(Modifier.height(10.dp))
    Text(label, color = colors.textMuted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    Spacer(Modifier.height(4.dp))
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (id in ids) {
            Box(Modifier.clip(RoundedCornerShape(6.dp)).background(color.copy(alpha = 0.16f)).padding(horizontal = 8.dp, vertical = 3.dp)) {
                Text(id, color = color, fontSize = 11.sp)
            }
        }
    }
}

// ── Shared bits ───────────────────────────────────────────────────────────────────────────────

private fun formatUsd(v: Double): String {
    // Deterministic 4-dp format without java.String.format (commonMain-safe).
    val cents = kotlin.math.round(v * 10000.0).toLong()
    val whole = cents / 10000
    val frac = (cents % 10000).toString().padStart(4, '0')
    return "$$whole.$frac"
}

@Composable
private fun StageTab(label: String, selected: Boolean, onClick: () -> Unit) {
    val colors = LocalBoBClawColors
    Box(
        modifier = Modifier.clip(RoundedCornerShape(8.dp))
            .background(if (selected) colors.surfaceAccent else colors.surfaceCard)
            .border(1.dp, if (selected) colors.accent else Color.Transparent, RoundedCornerShape(8.dp))
            .clickable(onClick = onClick).padding(horizontal = 14.dp, vertical = 6.dp),
    ) {
        Text(label, color = if (selected) colors.accent else colors.textSecondary, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun PrimaryButton(label: String, enabled: Boolean, onClick: () -> Unit) {
    val colors = LocalBoBClawColors
    Box(
        modifier = Modifier.clip(RoundedCornerShape(8.dp))
            .background(colors.accent.copy(alpha = if (enabled) 0.9f else 0.25f))
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 8.dp),
    ) {
        Text(label, color = colors.canvas, fontSize = 12.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun GhostButton(label: String, onClick: () -> Unit) {
    val colors = LocalBoBClawColors
    Box(
        modifier = Modifier.clip(RoundedCornerShape(8.dp)).background(colors.surfaceCard)
            .border(1.dp, colors.accent.copy(alpha = 0.4f), RoundedCornerShape(8.dp))
            .clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 8.dp),
    ) {
        Text(label, color = colors.accent, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
    }
}
