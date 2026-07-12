package com.bobclaw.ui.screens

import com.bobclaw.shared.resources.Res
import com.bobclaw.shared.resources.memory_askbob
import com.bobclaw.shared.resources.memory_close
import com.bobclaw.shared.resources.memory_empty
import com.bobclaw.shared.resources.memory_error
import com.bobclaw.shared.resources.memory_filters
import com.bobclaw.shared.resources.memory_forget
import com.bobclaw.shared.resources.memory_forget_cancel
import com.bobclaw.shared.resources.memory_forget_confirm
import com.bobclaw.shared.resources.memory_forget_yes
import com.bobclaw.shared.resources.memory_forgetting
import com.bobclaw.shared.resources.memory_forgotten
import com.bobclaw.shared.resources.memory_inspect_neighbors
import com.bobclaw.shared.resources.memory_inspect_no_neighbors
import com.bobclaw.shared.resources.memory_inspect_provenance
import com.bobclaw.shared.resources.memory_loading
import com.bobclaw.shared.resources.memory_no_match
import com.bobclaw.shared.resources.memory_refresh
import com.bobclaw.shared.resources.memory_search
import com.bobclaw.shared.resources.memory_search_hint
import com.bobclaw.shared.resources.memory_stats
import com.bobclaw.shared.resources.memory_subtitle
import com.bobclaw.shared.resources.memory_title
import com.bobclaw.shared.resources.memory_truncated
import com.bobclaw.shared.resources.memory_type_conversation
import com.bobclaw.shared.resources.memory_type_fact
import com.bobclaw.shared.resources.memory_warnings
import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
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
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.bobclaw.model.MemoryGraph
import com.bobclaw.model.MemoryNode
import com.bobclaw.model.NODE_CONVERSATION
import com.bobclaw.model.NODE_FACT
import com.bobclaw.model.filterByTypes
import com.bobclaw.model.findNode
import com.bobclaw.model.neighborsOf
import com.bobclaw.model.nodeById
import com.bobclaw.model.provenanceOf
import com.bobclaw.model.substrateTypes
import com.bobclaw.model.toRenderJson
import com.bobclaw.model.Capabilities
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.RestClient
import com.bobclaw.ui.components.AskBobBubble
import com.bobclaw.ui.components.AskBobPlacement
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Memory 3D screen (SPEC-UI-OVERHAUL §4 / §7 U4b / D9) — "the wow". Fetches the live
 * read-only `GET /memory/graph` (U4a), renders it in the embedded-Chromium canvas via the
 * injected [graphRenderer] (desktop = JCEF; bundled three.js + 3d-force-graph, no CDN), and
 * wraps it with the Kotlin-side interaction surface: per-substrate filters, search-and-fly-to,
 * an inspect panel (full text + provenance + nearest neighbours), and Forget.
 *
 * Fence (U4b accept #5): view + Forget only. The ONLY mutation is the existing gateway op
 * `DELETE /memory/facts/{id}` (fact nodes only) — no other memory writes/edits.
 */
@Composable
fun MemoryScreen(
    restClient: RestClient?,
    graphRenderer: MemoryGraphRenderer,
    // MS9-UD: Ask-Bob dock wiring (threaded from App.kt). The heavyweight JCEF canvas paints OVER
    // Compose, so a FLOATING bubble is occluded here; Ask Bob is instead DOCKED as a right-side panel
    // that SHRINKS the canvas (mirrors the Inspect panel). All optional so the screen still renders
    // standalone (previews/tests) without a live WS — the dock toggle appears only when wired.
    webSocket: BoBClawWebSocket? = null,
    capabilities: Capabilities? = null,
    confirmedActions: Set<String> = emptySet(),
    onConfirmAction: (String) -> Unit = {},
    onOpenApprovals: () -> Unit = {},
    askBobFaceId: String? = null,
    voiceBeta: Boolean = false,
    modifier: Modifier = Modifier,
) {
    // MS9-UD: is the docked Ask-Bob panel open? Only meaningful when wired (webSocket + restClient).
    val askBobEnabled = webSocket != null && restClient != null
    var askBobOpen by remember { mutableStateOf(false) }
    var graph by remember { mutableStateOf<MemoryGraph?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshNonce by remember { mutableStateOf(0) }

    var enabledTypes by remember { mutableStateOf<Set<String>>(emptySet()) }
    var search by remember { mutableStateOf("") }
    var searchMiss by remember { mutableStateOf<String?>(null) }
    var selectedId by remember { mutableStateOf<String?>(null) }
    var oneShot by remember { mutableStateOf<GraphOneShot?>(null) }
    var oneShotSeq by remember { mutableStateOf(0L) }
    var forgetPending by remember { mutableStateOf(false) }
    var forgetting by remember { mutableStateOf(false) }
    var toast by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(restClient, refreshNonce) {
        if (restClient == null) {
            loading = false
            error = "Not configured — no gateway URL set"
            return@LaunchedEffect
        }
        loading = true
        error = null
        try {
            val g = restClient.getMemoryGraph()
            graph = g
            enabledTypes = g.substrateTypes().toSet()  // filters default: every substrate on
            selectedId = selectedId?.takeIf { g.nodeById(it) != null }
            loading = false
        } catch (e: Exception) {
            graph = null
            error = e.message ?: "Unknown error"
            loading = false
        }
    }

    // Auto-dismiss the consequence toast (D12 pattern).
    LaunchedEffect(toast) { if (toast != null) { delay(2600); toast = null } }

    val allTypes = graph?.substrateTypes() ?: emptyList()
    val visible = remember(graph, enabledTypes) { graph?.filterByTypes(enabledTypes) }
    val renderJson = remember(visible) { visible?.toRenderJson() }
    val selected = selectedId?.let { id -> graph?.nodeById(id) }

    fun fireOneShot(kind: GraphOneShot.Kind, id: String) {
        oneShotSeq += 1
        oneShot = GraphOneShot(oneShotSeq, kind, id)
    }

    fun runSearch() {
        searchMiss = null
        val q = search.trim()
        if (q.isEmpty()) return
        val hit = visible?.findNode(q)
        if (hit != null) {
            selectedId = hit.id
            forgetPending = false
            fireOneShot(GraphOneShot.Kind.FLY_TO, hit.id)
        } else {
            searchMiss = q
        }
    }

    val forgottenMsg = stringResource(Res.string.memory_forgotten)
    fun forget(node: MemoryNode) {
        val factId = node.factId ?: return
        scope.launch {
            forgetting = true
            try {
                restClient?.forgetFact(factId)
                // Local removal is the source of truth (fact stays gone without a refetch);
                // the incremental JS remove keeps the canvas in sync without a full relayout.
                graph = graph?.let { g ->
                    g.copy(
                        nodes = g.nodes.filterNot { it.id == node.id },
                        edges = g.edges.filterNot { it.source == node.id || it.target == node.id },
                    )
                }
                fireOneShot(GraphOneShot.Kind.REMOVE, node.id)
                selectedId = null
                forgetPending = false
                toast = forgottenMsg
            } catch (e: Exception) {
                toast = "Forget failed: ${e.message ?: "error"}"
            } finally {
                forgetting = false
            }
        }
    }

    val colors = LocalBoBClawColors
    GradientBackground(modifier = modifier) {
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            // ── Header: title + live counts + refresh ─────────────────────────────
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(stringResource(Res.string.memory_title), style = BoBClawType.title, color = colors.textPrimary)
                    Text(stringResource(Res.string.memory_subtitle), style = BoBClawType.monoCaption, color = colors.textSecondary)
                }
                visible?.let { v ->
                    Text(
                        stringResource(Res.string.memory_stats, v.nodes.size, v.edges.size),
                        style = BoBClawType.monoCaption,
                        color = colors.textMuted,
                    )
                    Spacer(Modifier.width(12.dp))
                }
                Button(
                    onClick = { refreshNonce += 1 },
                    enabled = !loading,
                    colors = ButtonDefaults.buttonColors(containerColor = colors.accent, contentColor = colors.onAccent),
                ) { Text(stringResource(Res.string.memory_refresh)) }
            }

            // Honesty: cap + assembly warnings.
            graph?.meta?.let { m ->
                if (m.truncated || m.warnings.isNotEmpty()) {
                    Spacer(Modifier.height(6.dp))
                    val notes = buildList {
                        if (m.truncated) add(stringResource(Res.string.memory_truncated, m.nodeCap))
                        if (m.warnings.isNotEmpty()) add(stringResource(Res.string.memory_warnings, m.warnings.size))
                    }
                    Text(notes.joinToString("  ·  "), style = BoBClawType.monoCaption, color = colors.warn)
                }
            }

            Spacer(Modifier.height(12.dp))

            // ── Controls: search + per-substrate filters ──────────────────────────
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = search,
                    onValueChange = { search = it; searchMiss = null },
                    singleLine = true,
                    placeholder = { Text(stringResource(Res.string.memory_search_hint)) },
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                    keyboardActions = KeyboardActions(onSearch = { runSearch() }),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = colors.textPrimary,
                        unfocusedTextColor = colors.textPrimary,
                        focusedBorderColor = colors.accent,
                        unfocusedBorderColor = colors.borderControl,
                        cursorColor = colors.accent,
                    ),
                    modifier = Modifier
                        .weight(1f)
                        .onPreviewKeyEvent { ev ->
                            if (ev.type == KeyEventType.KeyDown && ev.key == Key.Enter) { runSearch(); true } else false
                        },
                )
                Spacer(Modifier.width(8.dp))
                Button(
                    onClick = { runSearch() },
                    enabled = search.isNotBlank(),
                    colors = ButtonDefaults.buttonColors(containerColor = colors.accent, contentColor = colors.onAccent),
                ) { Text(stringResource(Res.string.memory_search)) }
                // MS9-UD: docked Ask-Bob toggle beside Search — opening it SHRINKS the canvas (below).
                if (askBobEnabled) {
                    Spacer(Modifier.width(8.dp))
                    if (askBobOpen) {
                        Button(
                            onClick = { askBobOpen = false },
                            colors = ButtonDefaults.buttonColors(containerColor = colors.accent, contentColor = colors.onAccent),
                        ) { Text(stringResource(Res.string.memory_askbob)) }
                    } else {
                        OutlinedButton(
                            onClick = { askBobOpen = true },
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = colors.accent),
                        ) { Text(stringResource(Res.string.memory_askbob)) }
                    }
                }
            }

            if (allTypes.isNotEmpty()) {
                Spacer(Modifier.height(10.dp))
                Row(
                    modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(stringResource(Res.string.memory_filters), style = BoBClawType.monoCaption, color = colors.textMuted)
                    allTypes.forEach { t ->
                        // Live count (accurate after a Forget) — not the server's pre-forget meta.
                        val count = graph?.nodes?.count { it.type == t } ?: 0
                        FilterChip(
                            label = "${typeLabel(t)} ($count)",
                            active = t in enabledTypes,
                            dotColor = typeColor(t),
                        ) {
                            enabledTypes = if (t in enabledTypes) enabledTypes - t else enabledTypes + t
                        }
                    }
                }
            }

            searchMiss?.let {
                Spacer(Modifier.height(6.dp))
                Text(stringResource(Res.string.memory_no_match, it), style = BoBClawType.monoCaption, color = colors.warn)
            }

            Spacer(Modifier.height(12.dp))

            // ── Body: 3D canvas (+ inspect panel) (+ Ask-Bob dock) ────────────────
            Box(modifier = Modifier.fillMaxSize()) {
                // MS9-UD: the body is a Row so the docked Ask-Bob panel is a fixed-width sibling;
                // the body region carries `weight(1f)`, so it (and the JCEF canvas inside it) SHRINKS
                // when the dock opens — mirroring exactly how the Inspect panel shrinks the canvas
                // (verify #1). The dock is thus never painted over by the heavyweight canvas.
                Row(modifier = Modifier.fillMaxSize()) {
                    Box(modifier = Modifier.weight(1f).fillMaxHeight()) {
                        when {
                            loading && graph == null ->
                                Centered { Text(stringResource(Res.string.memory_loading), style = BoBClawType.body, color = colors.textSecondary) }
                            error != null && graph == null ->
                                Centered { Text(stringResource(Res.string.memory_error, error ?: ""), style = BoBClawType.body, color = colors.alert) }
                            graph != null && graph!!.nodes.isEmpty() ->
                                Centered { Text(stringResource(Res.string.memory_empty), style = BoBClawType.body, color = colors.textSecondary) }
                            else ->
                                Row(modifier = Modifier.fillMaxSize()) {
                                    Box(
                                        modifier = Modifier
                                            .weight(1f)
                                            .fillMaxHeight()
                                            .clip(BoBClawShapes.card)
                                            .background(Color(0xFF0B0F14), BoBClawShapes.card),
                                    ) {
                                        graphRenderer(
                                            renderJson,
                                            oneShot,
                                            { id -> selectedId = id; forgetPending = false },
                                            Modifier.fillMaxSize(),
                                        )
                                    }
                                    selected?.let { node ->
                                        Spacer(Modifier.width(12.dp))
                                        InspectPanel(
                                            node = node,
                                            graph = graph!!,
                                            forgetPending = forgetPending,
                                            forgetting = forgetting,
                                            onClose = { selectedId = null; forgetPending = false },
                                            onForgetRequest = { forgetPending = true },
                                            onForgetCancel = { forgetPending = false },
                                            onForgetConfirm = { forget(node) },
                                            modifier = Modifier.width(320.dp).fillMaxHeight(),
                                        )
                                    }
                                }
                        }
                    }

                    // MS9-UD: the docked Ask-Bob panel. Same page scope ("memory") + U3 action scope +
                    // D12 guardrails as the floating bubble — it reuses the SAME AskBobBubble machinery
                    // via placement = DOCKED (verify #2), never a duplicated chat/action path.
                    if (askBobOpen && askBobEnabled) {
                        Spacer(Modifier.width(12.dp))
                        AskBobBubble(
                            page = "memory",
                            pageSnapshot = {
                                "Screen: memory. Nodes: ${graph?.nodes?.size ?: 0}, edges: ${graph?.edges?.size ?: 0}."
                            },
                            webSocket = webSocket!!,
                            restClient = restClient!!,
                            capabilities = capabilities,
                            faceId = askBobFaceId,
                            confirmedActions = confirmedActions,
                            onConfirmAction = onConfirmAction,
                            onOpenApprovals = onOpenApprovals,
                            voiceBeta = voiceBeta,
                            placement = AskBobPlacement.DOCKED,
                            onClose = { askBobOpen = false },
                            modifier = Modifier.width(340.dp).fillMaxHeight(),
                        )
                    }
                }

                toast?.let {
                    Box(
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .padding(bottom = 16.dp)
                            .clip(BoBClawShapes.pill)
                            .background(colors.surfaceRaised, BoBClawShapes.pill)
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                    ) {
                        Text(it, style = BoBClawType.monoCaption, color = colors.textPrimary)
                    }
                }
            }
        }
    }
}

// ── Inspect panel ────────────────────────────────────────────────────────────

@Composable
private fun InspectPanel(
    node: MemoryNode,
    graph: MemoryGraph,
    forgetPending: Boolean,
    forgetting: Boolean,
    onClose: () -> Unit,
    onForgetRequest: () -> Unit,
    onForgetCancel: () -> Unit,
    onForgetConfirm: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = LocalBoBClawColors
    val neighbors = remember(graph, node.id) { graph.neighborsOf(node.id) }
    val provenance = remember(graph, node.id) { graph.provenanceOf(node.id) }

    Column(
        modifier = modifier
            .clip(BoBClawShapes.card)
            .background(colors.surfaceCard, BoBClawShapes.card)
            .padding(14.dp)
            .verticalScroll(rememberScrollState()),
    ) {
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .clip(BoBClawShapes.full)
                    .background(typeColor(node.type), BoBClawShapes.full)
                    .padding(horizontal = 10.dp, vertical = 3.dp),
            ) { Text(typeLabel(node.type), style = BoBClawType.monoCaption, color = Color(0xFF0B0F14), fontWeight = FontWeight.Bold) }
            Spacer(Modifier.weight(1f))
            OutlinedButton(
                onClick = onClose,
                colors = ButtonDefaults.outlinedButtonColors(contentColor = colors.textSecondary),
            ) { Text(stringResource(Res.string.memory_close)) }
        }

        Spacer(Modifier.height(10.dp))
        Text(node.displayText, style = BoBClawType.body, color = colors.textPrimary)

        provenance?.let { p ->
            Spacer(Modifier.height(14.dp))
            SectionLabel(stringResource(Res.string.memory_inspect_provenance))
            Spacer(Modifier.height(4.dp))
            Text(p.displayText, style = BoBClawType.monoCaption, color = colors.textSecondary)
        }

        if (node.type == NODE_FACT) {
            Spacer(Modifier.height(14.dp))
            SectionLabel(stringResource(Res.string.memory_inspect_neighbors))
            Spacer(Modifier.height(4.dp))
            if (neighbors.isEmpty()) {
                Text(stringResource(Res.string.memory_inspect_no_neighbors), style = BoBClawType.monoCaption, color = colors.textMuted)
            } else {
                neighbors.take(5).forEach { n ->
                    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
                        Text(
                            n.node.label,
                            style = BoBClawType.monoCaption,
                            color = colors.textSecondary,
                            modifier = Modifier.weight(1f),
                        )
                        n.weight?.let { w ->
                            Spacer(Modifier.width(8.dp))
                            Text(formatWeight(w), style = BoBClawType.monoCaption, color = colors.accent)
                        }
                    }
                }
            }
        }

        // Forget (fact nodes only; the existing DELETE /memory/facts op) — D12 confirm-once.
        if (node.forgettable) {
            Spacer(Modifier.height(16.dp))
            if (!forgetPending) {
                OutlinedButton(
                    onClick = onForgetRequest,
                    enabled = !forgetting,
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = colors.alert),
                    modifier = Modifier.fillMaxWidth(),
                ) { Text(if (forgetting) stringResource(Res.string.memory_forgetting) else stringResource(Res.string.memory_forget)) }
            } else {
                Text(stringResource(Res.string.memory_forget_confirm), style = BoBClawType.monoCaption, color = colors.warn)
                Spacer(Modifier.height(8.dp))
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = onForgetConfirm,
                        enabled = !forgetting,
                        colors = ButtonDefaults.buttonColors(containerColor = colors.alert, contentColor = colors.onAccent),
                        modifier = Modifier.weight(1f),
                    ) { Text(stringResource(Res.string.memory_forget_yes)) }
                    OutlinedButton(
                        onClick = onForgetCancel,
                        enabled = !forgetting,
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = colors.textSecondary),
                        modifier = Modifier.weight(1f),
                    ) { Text(stringResource(Res.string.memory_forget_cancel)) }
                }
            }
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(text, style = BoBClawType.monoCaption, color = LocalBoBClawColors.textMuted, fontWeight = FontWeight.Bold)
}

@Composable
private fun FilterChip(label: String, active: Boolean, dotColor: Color, onClick: () -> Unit) {
    val colors = LocalBoBClawColors
    Row(
        modifier = Modifier
            .clip(BoBClawShapes.full)
            .background(if (active) colors.surfaceAccent else colors.surfaceCard, BoBClawShapes.full)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .width(9.dp).height(9.dp)
                .clip(BoBClawShapes.full)
                .background(if (active) dotColor else colors.textMuted, BoBClawShapes.full),
        )
        Spacer(Modifier.width(6.dp))
        Text(label, style = BoBClawType.monoCaption, color = if (active) colors.textPrimary else colors.textMuted)
    }
}

@Composable
private fun Centered(content: @Composable () -> Unit) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { content() }
}

// ── Presentation helpers (mirror graph.html's type palette) ───────────────────

/** Friendly label for a substrate type; unknown collections show their raw name. */
@Composable
private fun typeLabel(type: String): String = when (type) {
    NODE_FACT -> stringResource(Res.string.memory_type_fact)
    NODE_CONVERSATION -> stringResource(Res.string.memory_type_conversation)
    else -> type
}

/** Node colour by type — kept in sync with graph.html's FIXED/hash palette. */
private fun typeColor(type: String): Color = when (type) {
    NODE_FACT -> Color(0xFF34D399)
    NODE_CONVERSATION -> Color(0xFF60A5FA)
    "research_forest" -> Color(0xFFF59E0B)
    else -> {
        var h = 0
        for (c in type) h = (h * 31 + c.code) and 0xFFFFFF
        Color.hsv((h % 360).toFloat(), 0.65f, 0.85f)
    }
}

private fun formatWeight(w: Double): String {
    val pct = (w * 100).toInt()
    return "$pct%"
}
