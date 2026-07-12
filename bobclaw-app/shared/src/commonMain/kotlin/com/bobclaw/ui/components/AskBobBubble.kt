package com.bobclaw.ui.components

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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.Action
import com.bobclaw.model.Capabilities
import com.bobclaw.model.PageContext
import com.bobclaw.model.ServerMessage
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.LocalBoBClawColors
import kotlinx.coroutines.launch

/**
 * U5 — the "Ask Bob" helper (SPEC §3 · D3). A slide-over mini-chat that rides the EXISTING chat WS
 * with page-scoped tool scope ([actionsForPage]) and the D11/D12 guardrails. Two modes:
 *   · **Guide** — the user types a question; the bubble sends it with a [PageContext] snapshot of the
 *     current screen (core splices it as a front-adjacent system card, flag-gated) so Bob can answer
 *     about what's on screen.
 *   · **Do** — page-scoped registry actions ([actionsForPage]) show as chips. Tapping one runs the
 *     D11-tier + D12-guardrail flow ([dispositionFor]): `read`/confirmed-`reversible` execute with a
 *     consequence toast; a `reversible` on first use confirms once (persisted); a `gated` action is
 *     routed to Approvals and NEVER executed here; a per-turn rate cap refuses runaway mutations.
 *
 * ### Placement (MS9-UD)
 * The SAME chat/action/guardrail machinery (state + [send]/[runAction]/[onPickAction]/WS-collect/
 * confirm dialog) drives two [AskBobPlacement] modes — the panel body ([AskBobPanel]) is shared, only
 * the outer chrome differs:
 *   · [AskBobPlacement.FLOATING] (default) — a collapsed pill bottom-right that expands into a floating
 *     card. Used on ordinary Compose pages (mounted by `App.kt`).
 *   · [AskBobPlacement.DOCKED] — no pill; the panel is always shown and fills its slot. Mounted INSIDE
 *     a screen as a right-side panel that shrinks a heavyweight JCEF canvas (Memory), which would
 *     otherwise paint OVER a floating bubble. [onClose] toggles the dock closed in the host screen.
 *
 * The tool-capable face is presented simply as "Bob". Execution of a Do-mode action rides Bob's own
 * tool loop over the WS (the registry IS Bob's tool scope) — the app-side guardrails gate the send.
 */
@Composable
fun AskBobBubble(
    page: String,
    pageSnapshot: () -> String,
    webSocket: BoBClawWebSocket,
    restClient: RestClient,
    capabilities: Capabilities?,
    faceId: String?,
    confirmedActions: Set<String>,
    onConfirmAction: (String) -> Unit,
    onOpenApprovals: () -> Unit,
    // U11 (SPEC §7): the `voice_beta` preview flag. OFF (default) ⇒ no mic emitted (byte-identical);
    // ON ⇒ an inert disabled mic ("coming soon") renders in the bubble's Guide-mode input row.
    voiceBeta: Boolean = false,
    // MS9-UD: FLOATING (default, App-mounted) vs DOCKED (screen-mounted shrinking side panel).
    placement: AskBobPlacement = AskBobPlacement.FLOATING,
    // MS9-UD: DOCKED-only — the host screen toggles the dock closed (ignored when FLOATING).
    onClose: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = LocalBoBClawColors
    val scope = rememberCoroutineScope()

    val docked = placement == AskBobPlacement.DOCKED
    // FLOATING: the pill toggles the panel. DOCKED: the panel is always shown (the HOST mounts/unmounts
    // it), so the shared WS-collect + panel run whenever this composable is in the tree.
    var expanded by remember { mutableStateOf(false) }
    val panelOpen = docked || expanded

    var input by remember { mutableStateOf("") }
    var awaiting by remember { mutableStateOf(false) }
    var toast by remember { mutableStateOf<String?>(null) }
    var pendingConfirm by remember { mutableStateOf<Action?>(null) }
    var helperConvId by remember { mutableStateOf<String?>(null) }
    // Mutating actions fired in the current user turn — reset when the user starts a fresh ask.
    var mutatingThisTurn by remember { mutableStateOf(0) }
    val transcript = remember { mutableStateListOf<BubbleLine>() }

    val actions = remember(capabilities, page) { actionsForPage(capabilities, page) }

    // Collect the shared WS stream while the panel is open; route chunks to the streaming line.
    androidx.compose.runtime.LaunchedEffect(panelOpen) {
        if (!panelOpen) return@LaunchedEffect
        webSocket.incomingMessages.collect { msg ->
            if (!awaiting) return@collect
            when (msg) {
                is ServerMessage.Chunk -> appendToStreaming(transcript, msg.content)
                is ServerMessage.MessageComplete -> awaiting = false
                is ServerMessage.Error -> {
                    toast = "${msg.code}: ${msg.message}"
                    awaiting = false
                }
                else -> { /* other frames ignored by the helper */ }
            }
        }
    }

    suspend fun ensureConv(): String {
        helperConvId?.let { return it }
        val created = restClient.createConversation(title = "Ask Bob", faceId = faceId)
        helperConvId = created.id
        return created.id
    }

    fun send(text: String) {
        if (text.isBlank()) return
        transcript.add(BubbleLine(fromBob = false, text = text))
        transcript.add(BubbleLine(fromBob = true, text = "", streaming = true))
        awaiting = true
        mutatingThisTurn = 0 // a fresh user turn resets the per-turn mutation budget
        scope.launch {
            runCatching {
                val convId = ensureConv()
                webSocket.sendMessageWithPageContext(
                    conversationId = convId,
                    content = text,
                    faceId = faceId,
                    pageContext = PageContext(page = page, snapshot = pageSnapshot()),
                )
            }.onFailure {
                toast = "Couldn't reach Bob: ${it.message}"
                awaiting = false
            }
        }
    }

    fun runAction(action: Action) {
        toast = consequenceToast(action)
        if (isMutating(action)) mutatingThisTurn += 1
        // Do-mode: ask Bob to perform the action; the registry is Bob's tool scope. Guardrails
        // (tier/confirm/rate cap) have already been applied before we get here.
        send("Please ${action.title.lowercase()} — ${action.descriptionPlain}")
    }

    fun onPickAction(action: Action) {
        when (dispositionFor(action, confirmedActions, mutatingThisTurn)) {
            ActionDisposition.EXECUTE -> runAction(action)
            ActionDisposition.CONFIRM_FIRST -> pendingConfirm = action
            ActionDisposition.ROUTE_TO_APPROVALS -> {
                toast = "\"${action.title}\" needs your OK — sent to Approvals."
                onOpenApprovals()
            }
            ActionDisposition.RATE_CAPPED ->
                toast = "That's a lot of changes at once — ask again to continue."
        }
    }

    // The panel body is shared across placements; only the outer container/chrome differs.
    val onSend: () -> Unit = {
        val text = input.trim()
        input = ""
        send(text)
    }

    when (placement) {
        // ── FLOATING: collapsed pill bottom-right → floating slide-over card ─────────
        AskBobPlacement.FLOATING -> Box(modifier = modifier.fillMaxSize()) {
            if (!expanded) {
                Box(
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(20.dp)
                        .clip(RoundedCornerShape(24.dp))
                        .background(colors.accent)
                        .clickable { expanded = true }
                        .padding(horizontal = 18.dp, vertical = 12.dp),
                ) {
                    Text("Ask Bob", color = colors.onAccent, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                }
            } else {
                AskBobPanel(
                    page = page,
                    actions = actions,
                    transcript = transcript,
                    input = input,
                    awaiting = awaiting,
                    toast = toast,
                    voiceBeta = voiceBeta,
                    onInputChange = { input = it },
                    onSend = onSend,
                    onPickAction = { onPickAction(it) },
                    onClose = { expanded = false },
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(16.dp)
                        .width(380.dp)
                        .heightIn(max = 560.dp),
                )
            }
        }

        // ── DOCKED: always-open right-side panel filling its slot (host shrinks the canvas) ──
        AskBobPlacement.DOCKED -> AskBobPanel(
            page = page,
            actions = actions,
            transcript = transcript,
            input = input,
            awaiting = awaiting,
            toast = toast,
            voiceBeta = voiceBeta,
            onInputChange = { input = it },
            onSend = onSend,
            onPickAction = { onPickAction(it) },
            onClose = onClose,
            modifier = modifier.fillMaxHeight(),
        )
    }

    // ── D12 confirm-once dialog (shared across placements) ─────────────────────────
    pendingConfirm?.let { action ->
        AlertDialog(
            onDismissRequest = { pendingConfirm = null },
            title = { Text("Let Bob ${action.title.lowercase()}?") },
            text = {
                Column {
                    Text(action.descriptionPlain)
                    action.undoHint?.let {
                        Spacer(Modifier.height(8.dp))
                        Text("You can undo: $it", fontSize = 12.sp)
                    }
                    Spacer(Modifier.height(8.dp))
                    Text("You'll only be asked this once.", fontSize = 12.sp)
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    onConfirmAction(action.id) // persist confirm-once
                    pendingConfirm = null
                    runAction(action)
                }) { Text("Yes, do it") }
            },
            dismissButton = {
                TextButton(onClick = { pendingConfirm = null }) { Text("Cancel") }
            },
        )
    }
}

/**
 * The shared Ask-Bob panel body — header · Do-mode chips · consequence toast · transcript · Guide-mode
 * input. Placement-agnostic: [AskBobBubble] renders this for BOTH the FLOATING card and the DOCKED
 * side panel (the chat/action/guardrail logic stays in the one caller — this is pure presentation).
 */
@Composable
private fun AskBobPanel(
    page: String,
    actions: List<Action>,
    transcript: List<BubbleLine>,
    input: String,
    awaiting: Boolean,
    toast: String?,
    voiceBeta: Boolean,
    onInputChange: (String) -> Unit,
    onSend: () -> Unit,
    onPickAction: (Action) -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = LocalBoBClawColors
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(16.dp))
            .background(colors.surfaceCard)
            .border(1.dp, colors.borderCard, RoundedCornerShape(16.dp))
            .padding(14.dp),
    ) {
        // Header
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text("Ask Bob", color = colors.textPrimary, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                Text("Helping on: $page", color = colors.textSecondary, fontSize = 12.sp)
            }
            Text(
                "Close",
                color = colors.accent,
                fontSize = 13.sp,
                modifier = Modifier.clickable { onClose() }.padding(4.dp),
            )
        }

        Spacer(Modifier.height(10.dp))

        // Do-mode action chips (page-scoped tool scope)
        if (actions.isNotEmpty()) {
            Text("What I can do here", color = colors.textMuted, fontSize = 11.sp)
            Spacer(Modifier.height(6.dp))
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                for (action in actions) {
                    val gated = action.risk == RISK_GATED
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(if (gated) colors.surfaceAccent else colors.surfaceRaised)
                            .clickable { onPickAction(action) }
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                    ) {
                        Text(
                            if (gated) "${action.title}  (needs approval)" else action.title,
                            color = colors.textBody,
                            fontSize = 13.sp,
                        )
                    }
                }
            }
            Spacer(Modifier.height(10.dp))
        }

        // Consequence toast (D12)
        toast?.let {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .background(colors.surfaceAccent)
                    .padding(10.dp),
            ) { Text(it, color = colors.textBody, fontSize = 12.sp) }
            Spacer(Modifier.height(8.dp))
        }

        // Transcript
        LazyColumn(
            modifier = Modifier.fillMaxWidth().weight(1f),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(transcript) { line ->
                Text(
                    text = (if (line.fromBob) "Bob: " else "You: ") +
                        (if (line.streaming && line.text.isEmpty()) "…" else line.text),
                    color = if (line.fromBob) colors.textBody else colors.textSecondary,
                    fontSize = 13.sp,
                )
            }
        }

        Spacer(Modifier.height(8.dp))

        // Guide-mode input row
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            androidx.compose.material3.OutlinedTextField(
                value = input,
                onValueChange = onInputChange,
                modifier = Modifier.weight(1f),
                placeholder = { Text("Ask about this screen…", fontSize = 13.sp, color = colors.textMuted) },
                singleLine = true,
                // MS9-W4 (fix E): without explicit colors the field inherited Material3 defaults, so the
                // typed text rendered the same color as the box (invisible until highlighted). Match the
                // app's other inputs (ComposerBar) — a visible body text color on the panel surface.
                colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                    focusedContainerColor = colors.surfaceRaised,
                    unfocusedContainerColor = colors.surfaceRaised,
                    focusedTextColor = colors.textBody,
                    unfocusedTextColor = colors.textBody,
                    focusedBorderColor = colors.accent,
                    unfocusedBorderColor = colors.borderControl,
                    cursorColor = colors.accent,
                ),
            )
            // U11: inert voice-input affordance — only when voice_beta is on (byte-identical off).
            if (voiceAffordancesVisible(voiceBeta)) {
                Spacer(Modifier.width(8.dp))
                VoiceMicButton(tooltip = "coming soon", contentDescription = "Voice input")
            }
            Spacer(Modifier.width(8.dp))
            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(10.dp))
                    .background(if (awaiting) colors.surfaceRaised else colors.accent)
                    .clickable(enabled = !awaiting) { onSend() }
                    .padding(horizontal = 16.dp, vertical = 12.dp),
            ) {
                Text("Send", color = colors.onAccent, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
            }
        }
    }
}

/** One line in the bubble transcript. */
internal data class BubbleLine(
    val fromBob: Boolean,
    val text: String,
    val streaming: Boolean = false,
)

/** Append a streamed chunk to the last (streaming) Bob line, if any. */
private fun appendToStreaming(transcript: androidx.compose.runtime.snapshots.SnapshotStateList<BubbleLine>, chunk: String) {
    val idx = transcript.indexOfLast { it.fromBob && it.streaming }
    if (idx >= 0) transcript[idx] = transcript[idx].copy(text = transcript[idx].text + chunk)
}
