package com.bobclaw.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.ApprovalItem
import com.bobclaw.model.ApprovalKind
import com.bobclaw.model.ServerMessage
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.RestClient
import com.bobclaw.ui.components.IconGlyph
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.onSubscription
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

private val DenyRed = Color(0xFFE74C3C)
private const val LITERACY_FALLBACK = "Couldn't load an explanation right now — you can still decide below."

/**
 * U6 — the real Approvals surface (SPEC §7 · §6), replacing the placeholder. Binds to the EXISTING
 * gateway approvals REST: it lists pending approvals (`GET /approvals`, polled like the Home tile),
 * approves/denies them live (`POST /approvals/{id}/decide {approve|reject}`), and enriches each item
 * with the read-only kinds map (`GET /approvals/kinds`) for a friendly label + a proposal-only badge.
 *
 * **Literacy layer:** an on-demand plain-language explanation + pros/cons per item — a cheap
 * assistant-face call over the EXISTING chat WS (a dedicated ephemeral conversation; no new endpoint),
 * cached per approval id ([LiteracyCache]). Calibrated by [experienceLevel] (SPEC §6): **Simple**
 * auto-fetches the explanation and hides the raw diff; **Pro** fetches on click and renders the
 * `cc_edit` diff inline.
 *
 * Fence: DISPLAY only. No change to approval semantics, gates, or the decide contract.
 *
 * NOTE (shared-WS caveat, same as U5): chat chunks carry no conversation id, so the literacy fetch and
 * a concurrent main-chat / Ask-Bob-bubble stream can interleave. Fetches are serialized here by a
 * [Mutex]; cross-component interleave is a best-effort limitation — fetch with the bubble idle.
 */
@Composable
fun ApprovalsScreen(
    restClient: RestClient,
    webSocket: BoBClawWebSocket,
    experienceLevel: String,
    modifier: Modifier = Modifier,
) {
    val items = remember { mutableStateOf<List<ApprovalItem>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var kinds by remember { mutableStateOf<List<ApprovalKind>>(emptyList()) }

    val cache = remember { LiteracyCache() }
    val literacyMutex = remember { Mutex() }
    val helperConvId = remember { mutableStateOf<String?>(null) }

    // Live list — same 10s poll as ApprovalsTile so a decided item drops out and new ones appear.
    LaunchedEffect(restClient) {
        while (true) {
            try {
                items.value = restClient.getApprovals()
                error = null
            } catch (e: Exception) {
                if (items.value == null) error = e.message
            }
            loading = false
            delay(10_000)
        }
    }

    // Kind labels — fetched once, fail-soft to empty (the surface stays kind-agnostic without them).
    LaunchedEffect(restClient) {
        kinds = runCatching { restClient.getApprovalKinds() }.getOrDefault(emptyList())
    }

    // The literacy face-call seam: reuse the chat WS via one ephemeral "Approval help" conversation;
    // serialize fetches (Mutex) and cache per approval id. Untested app-lane glue (Opus-authored).
    val explainer = remember(restClient, webSocket) {
        ApprovalExplainer { approval, level ->
            cache.get(approval.id)?.let { return@ApprovalExplainer it }
            literacyMutex.withLock {
                cache.get(approval.id)?.let { return@withLock it }
                val convId = helperConvId.value
                    ?: restClient.createConversation(title = "Approval help", faceId = null).id
                        .also { helperConvId.value = it }
                val prompt = literacyPrompt(approval, level)
                val reply = collectOneShotReply(webSocket, convId, prompt, faceId = null)
                    .ifBlank { LITERACY_FALLBACK }
                cache.put(approval.id, reply)
                reply
            }
        }
    }

    val onDecide: suspend (String, String) -> Unit = { id, decision ->
        restClient.postApprovalDecision(id, decision)
        items.value = items.value?.filterNot { it.id == id } // optimistic drop; poll reconciles
    }

    val colors = LocalBoBClawColors
    val pending = items.value?.filter { it.status == "pending" } ?: emptyList()

    GradientBackground(modifier = modifier) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(20.dp)
                .verticalScroll(rememberScrollState())
        ) {
            Text("Approvals", color = colors.textPrimary, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(4.dp))
            Text(
                text = when {
                    loading && items.value == null -> "Loading…"
                    error != null && items.value == null -> "Couldn't reach the gateway: $error"
                    pending.isEmpty() -> "No pending approvals — you're all caught up."
                    else -> "${pending.size} waiting for your decision" +
                        (if (experienceLevel == EXPERIENCE_PRO) " · Pro" else " · Simple")
                },
                color = colors.textSecondary,
                fontSize = 12.sp,
            )
            Spacer(Modifier.height(16.dp))

            for (approval in pending) {
                ApprovalRow(
                    approval = approval,
                    kinds = kinds,
                    experienceLevel = experienceLevel,
                    explainer = explainer,
                    onDecide = onDecide,
                )
                Spacer(Modifier.height(12.dp))
            }
        }
    }
}

@Composable
private fun ApprovalRow(
    approval: ApprovalItem,
    kinds: List<ApprovalKind>,
    experienceLevel: String,
    explainer: ApprovalExplainer,
    onDecide: suspend (String, String) -> Unit,
) {
    val colors = LocalBoBClawColors
    val scope = rememberCoroutineScope()

    var explanation by remember(approval.id) { mutableStateOf<String?>(null) }
    var explaining by remember(approval.id) { mutableStateOf(false) }
    var decision by remember(approval.id) { mutableStateOf<String?>(null) }

    val label = kindLabelFor(kinds, approval.actionType)
    val kindDesc = kindDescriptionFor(kinds, approval.actionType)
    val proposalOnly = isProposalOnly(kinds, approval.actionType)
    val summary = approvalSummary(approval)
    val diff = ccEditDiff(approval)

    fun fetchExplanation() {
        if (explaining || explanation != null) return
        explaining = true
        scope.launch {
            explanation = runCatching { explainer.explain(approval, experienceLevel) }
                .getOrElse { LITERACY_FALLBACK }
            explaining = false
        }
    }

    // Simple auto-fetches; Pro waits for the click. Re-keyed on id so a new item fetches once.
    LaunchedEffect(approval.id, experienceLevel) {
        if (shouldAutoFetchLiteracy(experienceLevel)) fetchExplanation()
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(colors.surfaceCard)
            .padding(14.dp),
    ) {
        // ── Header: friendly label + proposal-only badge ──────────────────────────
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(label, color = colors.accent, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
            if (proposalOnly) {
                Spacer(Modifier.width(8.dp))
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(colors.surfaceAccent)
                        .padding(horizontal = 8.dp, vertical = 2.dp),
                ) {
                    Text("proposal · never auto-applies", color = colors.textSecondary, fontSize = 10.sp)
                }
            }
            Spacer(Modifier.weight(1f))
            Text(approval.actionType, color = colors.textMuted, fontSize = 10.sp)
        }

        if (kindDesc.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(kindDesc, color = colors.textSecondary, fontSize = 12.sp)
        }

        if (summary.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(
                summary,
                color = colors.textBody,
                fontSize = 12.sp,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
        }

        // ── cc_edit diff (Pro only) ───────────────────────────────────────────────
        if (diff != null && showRawDiff(experienceLevel)) {
            Spacer(Modifier.height(10.dp))
            Text("Proposed changes", color = colors.textMuted, fontSize = 11.sp)
            Spacer(Modifier.height(4.dp))
            DiffBlock(diff)
        }

        // ── Literacy explanation ──────────────────────────────────────────────────
        Spacer(Modifier.height(10.dp))
        when {
            explanation != null -> {
                Text("In plain language", color = colors.textMuted, fontSize = 11.sp)
                Spacer(Modifier.height(4.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(8.dp))
                        .background(colors.surfaceRaised)
                        .heightIn(max = 220.dp)
                        .verticalScroll(rememberScrollState())
                        .padding(10.dp),
                ) {
                    Text(explanation!!, color = colors.textBody, fontSize = 12.sp)
                }
            }
            explaining -> Text("Bob is explaining this…", color = colors.textSecondary, fontSize = 12.sp)
            // Pro: on-demand. Simple with a load failure also lands here → offer a retry.
            else -> Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .background(colors.surfaceAccent)
                    .clickable { fetchExplanation() }
                    .padding(horizontal = 12.dp, vertical = 6.dp),
            ) {
                Text("Explain this in plain language", color = colors.accent, fontSize = 12.sp)
            }
        }

        // ── Approve / Deny (the decide contract — display-layer only) ─────────────
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            DecisionButton(
                label = "Approve",
                tint = colors.success,
                enabled = decision == null,
            ) {
                decision = APPROVAL_DECISION_APPROVE
                scope.launch {
                    runCatching { onDecide(approval.id, APPROVAL_DECISION_APPROVE) }
                        .onFailure { decision = null } // revert so the user can retry
                }
            }
            DecisionButton(
                label = "Deny",
                tint = DenyRed,
                enabled = decision == null,
            ) {
                decision = APPROVAL_DECISION_REJECT
                scope.launch {
                    runCatching { onDecide(approval.id, APPROVAL_DECISION_REJECT) }
                        .onFailure { decision = null }
                }
            }
            if (decision != null) {
                Spacer(Modifier.width(4.dp))
                IconGlyph(
                    name = if (decision == APPROVAL_DECISION_APPROVE) "checks" else "x",
                    tint = if (decision == APPROVAL_DECISION_APPROVE) colors.success else DenyRed,
                    size = 14.dp,
                )
            }
        }
    }
}

@Composable
private fun DecisionButton(label: String, tint: Color, enabled: Boolean, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(6.dp))
            .background(tint.copy(alpha = if (enabled) 0.15f else 0.06f))
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 6.dp),
    ) {
        Text(label, color = tint.copy(alpha = if (enabled) 1f else 0.5f), fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
    }
}

/** Render a unified diff with +/- line tinting (Pro `cc_edit` view). Mono, scrollable both axes. */
@Composable
private fun DiffBlock(diff: String) {
    val colors = LocalBoBClawColors
    val addGreen = colors.success
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(colors.surfaceRaised)
            .heightIn(max = 260.dp)
            .verticalScroll(rememberScrollState())
            .padding(10.dp),
    ) {
        Column(modifier = Modifier.horizontalScroll(rememberScrollState())) {
            for (line in diff.split("\n")) {
                val color = when {
                    line.startsWith("+") && !line.startsWith("+++") -> addGreen
                    line.startsWith("-") && !line.startsWith("---") -> DenyRed
                    line.startsWith("@@") -> colors.accent
                    else -> colors.textBody
                }
                Text(
                    text = if (line.isEmpty()) " " else line,
                    color = color,
                    fontSize = 11.sp,
                    fontFamily = FontFamily.Monospace,
                    maxLines = 1,
                )
            }
        }
    }
}

/** Sentinel to break out of the shared-WS collect once the reply completes (or errors). */
private object StopCollecting : Throwable()

/**
 * Send one prompt over the EXISTING chat WS and accumulate the streamed reply until `message_complete`
 * (or an error / [timeoutMs]). Subscribes BEFORE sending ([onSubscription]) so the SharedFlow (replay=0)
 * can't drop the reply. Untested app-lane glue: chunks carry no conversation id, so a concurrent stream
 * can interleave — callers serialize with a Mutex and treat the result as best-effort display text.
 */
private suspend fun collectOneShotReply(
    webSocket: BoBClawWebSocket,
    conversationId: String,
    prompt: String,
    faceId: String?,
    timeoutMs: Long = 60_000L,
): String {
    val sb = StringBuilder()
    try {
        withTimeoutOrNull(timeoutMs) {
            webSocket.incomingMessages
                .onSubscription { webSocket.sendMessage(conversationId, prompt, faceId) }
                .collect { msg ->
                    when (msg) {
                        is ServerMessage.Chunk -> sb.append(msg.content)
                        is ServerMessage.MessageComplete -> throw StopCollecting
                        is ServerMessage.Error -> throw StopCollecting
                        else -> { /* other frames ignored */ }
                    }
                }
        }
    } catch (_: StopCollecting) {
        // normal terminal — the reply finished (or the server sent an error frame)
    }
    return sb.toString().trim()
}
