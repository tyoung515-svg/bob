package com.bobclaw.ui.screens

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import kotlinx.datetime.Clock
import kotlinx.datetime.Instant
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime
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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
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
import androidx.compose.ui.input.key.isShiftPressed
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.auth.AuthManager
import com.bobclaw.model.Capabilities
import com.bobclaw.model.Conversation
import com.bobclaw.model.Face
import com.bobclaw.model.ProjectSummary
import com.bobclaw.model.ServerMessage
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.RestClient
import com.bobclaw.ui.MarkdownText
import com.bobclaw.ui.extractFileArtifact
import com.bobclaw.ui.extractHtmlArtifact
import com.bobclaw.ui.components.IconGlyph
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import com.bobclaw.ui.theme.glassMorphism
import kotlinx.coroutines.launch

/** Local chat-bubble model (decoupled from the persisted Message wire type). */
internal data class ChatBubble(
    val id: String,
    val role: String,        // "user" | "assistant"
    val content: String,
    val streaming: Boolean = false,
    val timestamp: String? = null,   // ISO instant — createdAt for history, client now for live
)

private const val DEFAULT_FACE_ID = "planner-claude"

/**
 * Remove the trailing optimistic assistant placeholder for [streamingId] iff it never received
 * any content — so an errored turn leaves no blank "Assistant ..." bubble. Guards against removing
 * a non-empty (partially streamed) reply.
 */
private fun removeEmptyAssistantPlaceholder(bubbles: MutableList<ChatBubble>, streamingId: String?) {
    val sid = streamingId ?: return
    val idx = bubbles.indexOfLast { it.id == sid }
    if (idx >= 0 && bubbles[idx].role == "assistant" && bubbles[idx].content.isEmpty()) {
        bubbles.removeAt(idx)
    }
}

/**
 * The chat payoff screen. On enter: connect the WS, collect [BoBClawWebSocket.incomingMessages],
 * load faces (pin planner-claude), pick/create a conversation, load history (newest-first → reversed).
 * Sends pin the selected face via `face_id` on the message frame; the gateway honors it.
 *
 * Layout is a Row: a ~260dp conversation sidebar + the chat column (weight 1f). The WS stays
 * connected across conversation switches — conversation_id is per-message, so we never reconnect.
 *
 * @param openConversationId optional id to select on enter (e.g. opened from the dashboard).
 *        Falls back to most-recent, else creates a new conversation.
 */
fun nextLocale(cur: String): String = when (cur) { "en" -> "zh-Hans"; "zh-Hans" -> "zh-Hant"; else -> "en" }

/** UI-only chat connection status — localized at render (carries any dynamic args). The values are
 * display-only; nothing is sent to the backend. */
sealed interface ChatStatus {
    object Connecting : ChatStatus
    object Connected : ChatStatus
    object Streaming : ChatStatus
    object Sending : ChatStatus
    data class Stopped(val code: String) : ChatStatus
    data class ConnectedStats(val tok: String, val ms: String) : ChatStatus
}

@Composable
private fun ChatStatus.label(): String = when (this) {
    ChatStatus.Connecting -> stringResource(Res.string.status_connecting)
    ChatStatus.Connected -> stringResource(Res.string.status_connected)
    ChatStatus.Streaming -> stringResource(Res.string.status_streaming)
    ChatStatus.Sending -> stringResource(Res.string.status_sending)
    is ChatStatus.Stopped -> stringResource(Res.string.status_stopped, code)
    is ChatStatus.ConnectedStats -> stringResource(Res.string.status_connected_stats, tok, ms)
}

@Composable
fun ChatScreen(
    authManager: AuthManager,
    restClient: RestClient,
    webSocket: BoBClawWebSocket,
    onLogout: () -> Unit,
    onOpenDashboard: (() -> Unit)? = null,
    openConversationId: String? = null,
    artifactRenderer: @Composable (html: String?, url: String?, modifier: Modifier) -> Unit = { _, _, _ -> },
    locale: String = "en",
    onSetLocale: (String) -> Unit = {},
    // U9 (SPEC §6): Simple vs Pro chat calibration. Pro renders exactly today's chips/placeholder
    // (byte-identical); Simple swaps the face pill for a plain-language mode picker, collapses the
    // jargon power chips (backend/profile) behind a "Details" affordance, and uses "Message Bob…".
    experienceLevel: String = "simple",
    // U11 (SPEC §7): the `voice_beta` preview flag, forwarded to the composer (mic) + message rows
    // (read-aloud). OFF (default) ⇒ chat surface byte-identical; ON ⇒ inert affordances render.
    voiceBeta: Boolean = false,
) {
    val scope = rememberCoroutineScope()

    val bubbles = remember { mutableStateListOf<ChatBubble>() }
    var faces by remember { mutableStateOf<List<Face>>(emptyList()) }
    // Live capability registry (faces/backends/capabilities) for the composer `/` palette (MS8-G1).
    // null until the GET /capabilities fetch lands (or if it fails) — the palette degrades gracefully.
    var capabilities by remember { mutableStateOf<Capabilities?>(null) }
    var selectedFaceId by remember { mutableStateOf(DEFAULT_FACE_ID) }
    // null = Auto / unpinned (clears the backend pin). Seeded from the opened conversation's pin.
    var selectedBackend by remember { mutableStateOf<String?>(null) }
    var conversationId by remember { mutableStateOf<String?>(null) }
    val conversations = remember { mutableStateListOf<Conversation>() }
    var status by remember { mutableStateOf<ChatStatus>(ChatStatus.Connecting) }
    var errorBanner by remember { mutableStateOf<String?>(null) }
    var input by remember { mutableStateOf("") }
    var generating by remember { mutableStateOf(false) }
    var faceMenuOpen by remember { mutableStateOf(false) }
    var backendMenuOpen by remember { mutableStateOf(false) }
    // U9: in Simple mode the jargon power chips (backend/profile) are collapsed behind "Details";
    // this toggles them visible on demand. In Pro they are always inline (this is ignored).
    var powerExpanded by remember { mutableStateOf(false) }
    // Profile (HOW layer) pin: null = off. Sends switch_profile; a council-shaped
    // profile then runs its role-prompted seats for this conversation.
    var selectedProfile by remember { mutableStateOf<String?>(null) }
    var profileMenuOpen by remember { mutableStateOf(false) }
    var profileNames by remember { mutableStateOf<List<String>>(emptyList()) }
    // Top-bar overflow menu (Dashboard / Log out) — keeps the bar narrow so it never jumbles.
    var menuOpen by remember { mutableStateOf(false) }
    // Collapse the conversation sidebar for a focused, full-width chat.
    var sidebarCollapsed by remember { mutableStateOf(false) }
    // Canvas (artifact) pane: inline HTML or a file:// URL to render + whether the pane is open.
    var canvasOpen by remember { mutableStateOf(false) }
    var canvasHtml by remember { mutableStateOf<String?>(null) }
    var canvasUrl by remember { mutableStateOf<String?>(null) }
    // Canvas URL bar: the editable address text (the canvas acts as a mini-browser).
    var canvasUrlInput by remember { mutableStateOf("") }

    // Normalize a typed address: blank → null (clears the canvas); add an https:// scheme if the
    // user omitted one. Used by the canvas URL bar's "Go" button and its Enter key handler.
    fun goToCanvasUrl(rawText: String) {
        val raw = rawText.trim()
        if (raw.isEmpty()) {
            canvasUrl = null
            canvasHtml = null
            return
        }
        val normalized = if (raw.contains("://")) raw else "https://$raw"
        println("[chat] canvas url -> $normalized")
        canvasUrl = normalized
        canvasHtml = null
    }

    // Rename dialog state (null = closed). Holds the conversation being renamed.
    var renameTarget by remember { mutableStateOf<Conversation?>(null) }
    var renameText by remember { mutableStateOf("") }

    // ---- server-side projects (workspaces; the gateway owns assignment) ----
    var projects by remember { mutableStateOf<List<ProjectSummary>>(emptyList()) }
    // Collapsed project ids (also "__unfiled__" for the Unfiled group). UI-only, not persisted.
    val collapsedProjectIds = remember { mutableStateListOf<String>() }
    // Create Project dialog: null = closed. Holds the in-progress field values.
    var createProjectDraft by remember { mutableStateOf<ProjectDraft?>(null) }
    // Edit Project dialog: null = closed. Holds the project id + prefilled (full) field values.
    var editProjectDraft by remember { mutableStateOf<EditProjectDraft?>(null) }

    // Refetch the project list (used after any project/conversation mutation).
    suspend fun refreshProjects() {
        projects = runCatching { restClient.getProjects() }.getOrDefault(emptyList())
    }

    // id of the assistant bubble currently being streamed into.
    var streamingId by remember { mutableStateOf<String?>(null) }
    val listState = rememberLazyListState()

    // Load a conversation's history into the bubble list (newest-first → reversed for oldest-at-top).
    suspend fun loadHistory(convId: String) {
        runCatching { restClient.getMessages(convId, limit = 50, before = null) }
            .onSuccess { page ->
                bubbles.clear()
                page.messages.reversed().forEach {
                    bubbles.add(ChatBubble(id = it.id, role = it.role, content = it.content, timestamp = it.createdAt))
                }
                status = ChatStatus.Connected
            }
            .onFailure { errorBanner = "Failed to load history: ${it.message}" }
    }

    // Switch the active conversation: reset stream state + repopulate bubbles. WS is NOT reconnected.
    fun switchTo(convId: String) {
        if (generating) {
            println("[chat] switch ignored (generating) -> $convId")
            return
        }
        if (convId == conversationId) return
        println("[chat] switch conversation -> $convId")
        conversationId = convId
        // Seed the backend picker from this conversation's stored pin (null = Auto / unpinned).
        selectedBackend = conversations.firstOrNull { it.id == convId }?.backendPreference
        streamingId = null
        generating = false
        bubbles.clear()
        scope.launch { loadHistory(convId) }
    }

    // Refetch the sidebar list (newest-first from the gateway) + the project list they group into.
    suspend fun refreshConversations() {
        runCatching { restClient.getConversations(limit = 30, offset = 0) }
            .onSuccess { convs ->
                conversations.clear()
                conversations.addAll(convs)
            }
            .onFailure { errorBanner = "Failed to load conversations: ${it.message}" }
        refreshProjects()
    }

    // --- WS lifecycle + stream collection -------------------------------------------------
    LaunchedEffect(Unit) {
        // 1) auth token + connect
        val token = authManager.getAccessToken()
        if (token == null) {
            errorBanner = "No access token — please log in again."
            return@LaunchedEffect
        }
        println("[chat] token present; connecting WS + collecting stream")
        webSocket.connect(token)

        // 2) collect the stream (lives for the composition lifetime)
        scope.launch {
            webSocket.incomingMessages.collect { msg ->
                when (msg) {
                    is ServerMessage.Chunk -> {
                        val sid = streamingId
                        if (sid != null) {
                            val idx = bubbles.indexOfLast { it.id == sid }
                            if (idx >= 0) {
                                bubbles[idx] = bubbles[idx].copy(content = bubbles[idx].content + msg.content)
                            } else {
                                println("[chat] chunk DROPPED: no bubble for sid=$sid")
                            }
                        } else {
                            println("[chat] chunk DROPPED: streamingId=null")
                        }
                        status = ChatStatus.Streaming
                    }
                    is ServerMessage.MessageComplete -> {
                        println("[chat] complete sid=$streamingId tok=${msg.tokensOut} ms=${msg.elapsedMs}")
                        val sid = streamingId
                        if (sid != null) {
                            val idx = bubbles.indexOfLast { it.id == sid }
                            if (idx >= 0) bubbles[idx] = bubbles[idx].copy(streaming = false)
                        }
                        streamingId = null
                        generating = false
                        status = ChatStatus.ConnectedStats(msg.tokensOut.toString(), msg.elapsedMs.toString())
                        // Refresh sidebar so the just-touched conversation gets its updated preview.
                        scope.launch { refreshConversations() }
                    }
                    is ServerMessage.Error -> {
                        println("[chat] error ${msg.code}: ${msg.message}")
                        errorBanner = "${msg.code}: ${msg.message}"
                        if (msg.code != "decode_error") {
                            // Drop the empty assistant placeholder so no blank "Assistant ..." bubble
                            // lingers (the red error banner already carries the message).
                            removeEmptyAssistantPlaceholder(bubbles, streamingId)
                            generating = false
                            streamingId = null
                        }
                    }
                    is ServerMessage.GenerationStopped -> {
                        val sid = streamingId
                        if (sid != null) {
                            val idx = bubbles.indexOfLast { it.id == sid }
                            // If nothing streamed in, drop the empty placeholder; otherwise just
                            // mark it done so the partial reply stays visible.
                            if (idx >= 0 && bubbles[idx].content.isEmpty()) bubbles.removeAt(idx)
                            else if (idx >= 0) bubbles[idx] = bubbles[idx].copy(streaming = false)
                        }
                        streamingId = null
                        generating = false
                        status = ChatStatus.Stopped(msg.code.toString())
                    }
                    is ServerMessage.FaceSwitched -> {
                        msg.faceId?.let { selectedFaceId = it }
                    }
                    else -> { /* ModelSwitched / ApprovalRequest — ignore for MVP */ }
                }
            }
        }

        // 3) faces (pin planner-claude, else fall back to first)
        runCatching { restClient.getFaces() }
            .onSuccess { loaded ->
                faces = loaded
                selectedFaceId = when {
                    loaded.any { it.id == DEFAULT_FACE_ID } -> DEFAULT_FACE_ID
                    loaded.isNotEmpty() -> loaded.first().id
                    else -> DEFAULT_FACE_ID
                }
            }
            .onFailure { errorBanner = "Failed to load faces: ${it.message}" }

        // 3b) profiles (HOW layer) — names for the chat profile picker.
        runCatching { restClient.getProfiles() }
            .onSuccess { loaded -> profileNames = loaded.map { it.name } }

        // 3c) capabilities registry (faces/backends/capabilities) for the composer `/` palette
        //     (MS8-G1, GET /capabilities). Fail-soft: a null document just means the palette shows
        //     the init action until the fetch lands; a partial outage still lists what it composed.
        runCatching { restClient.getCapabilities() }
            .onSuccess { capabilities = it }
            .onFailure { println("[chat] capabilities load failed: ${it.message}") }

        // 4) conversation list + selection:
        //    openConversationId (if present in the list) → most-recent → create one.
        runCatching { restClient.getConversations(limit = 30, offset = 0) }
            .onSuccess { convs ->
                conversations.clear()
                conversations.addAll(convs)

                val target = openConversationId?.let { id -> convs.firstOrNull { it.id == id } }
                    ?: convs.firstOrNull()
                    ?: restClient.createConversation(title = "New chat", faceId = selectedFaceId)
                        .also { conversations.add(0, it) }
                conversationId = target.id
                // Seed the backend picker from the opened conversation's stored pin (null = Auto).
                selectedBackend = target.backendPreference
                println("[chat] conversation=${target.id} (of ${convs.size}, requested=$openConversationId)")

                // 5) history (gateway is newest-first → reverse for oldest-at-top display)
                loadHistory(target.id)
            }
            .onFailure { errorBanner = "Failed to load conversations: ${it.message}" }

        // 6) projects (the sidebar groups conversations by these; fail-soft to an empty list)
        refreshProjects()
    }

    // auto-scroll to the newest bubble as content grows
    LaunchedEffect(bubbles.size, bubbles.lastOrNull()?.content) {
        if (bubbles.isNotEmpty()) listState.animateScrollToItem(bubbles.lastIndex)
    }

    fun newChat() {
        if (generating) {
            println("[chat] new chat ignored (generating)")
            return
        }
        println("[chat] new chat (face=$selectedFaceId)")
        scope.launch {
            runCatching { restClient.createConversation(title = "New chat", faceId = selectedFaceId) }
                .onSuccess { conv ->
                    conversations.add(0, conv)
                    conversationId = conv.id
                    // Brand-new chat: reset the backend picker to Auto (server has no pin yet).
                    selectedBackend = null
                    streamingId = null
                    generating = false
                    bubbles.clear()
                    status = ChatStatus.Connected
                    println("[chat] new chat created=${conv.id}")
                }
                .onFailure { errorBanner = "Failed to create conversation: ${it.message}" }
        }
    }

    fun rename(conv: Conversation, newTitle: String) {
        val title = newTitle.trim()
        if (title.isEmpty()) {
            println("[chat] rename ignored (blank) conv=${conv.id}")
            return
        }
        println("[chat] rename conv=${conv.id} -> '$title'")
        scope.launch {
            runCatching { restClient.renameConversation(conv.id, title) }
                .onSuccess { updated ->
                    val idx = conversations.indexOfFirst { it.id == updated.id }
                    if (idx >= 0) conversations[idx] = updated
                    refreshConversations()
                }
                .onFailure { errorBanner = "Rename failed: ${it.message}" }
        }
    }

    fun archive(conv: Conversation) {
        println("[chat] archive conv=${conv.id}")
        scope.launch {
            runCatching { restClient.archiveConversation(conv.id) }
                .onSuccess {
                    // Server owns project assignment — no local cleanup needed on archive.
                    val wasActive = conv.id == conversationId
                    conversations.removeAll { it.id == conv.id }
                    if (wasActive) {
                        val next = conversations.firstOrNull()
                        if (next != null) {
                            conversationId = next.id
                            selectedBackend = next.backendPreference
                            streamingId = null
                            generating = false
                            bubbles.clear()
                            loadHistory(next.id)
                        } else {
                            // nothing left — spin up a fresh conversation
                            val created = restClient.createConversation(title = "New chat", faceId = selectedFaceId)
                            conversations.add(0, created)
                            conversationId = created.id
                            selectedBackend = null
                            streamingId = null
                            generating = false
                            bubbles.clear()
                            status = ChatStatus.Connected
                        }
                    }
                    refreshConversations()
                }
                .onFailure { errorBanner = "Archive failed: ${it.message}" }
        }
    }

    // [rawText] defaults to the composer input; the `/init` palette action passes a canned prompt
    // so one-click init sends regardless of what's typed. Either way the composer is cleared.
    fun send(rawText: String = input) {
        val text = rawText.trim()
        val convId = conversationId
        if (text.isEmpty() || convId == null || generating) {
            println("[chat] send ignored (empty=${text.isEmpty()} convId=$convId generating=$generating)")
            return
        }
        // optimistic user bubble
        bubbles.add(ChatBubble(id = "u-${bubbles.size}-${text.hashCode()}", role = "user", content = text, timestamp = nowIso()))
        // empty assistant bubble to fill from Chunk frames
        val asstId = "a-${bubbles.size}"
        bubbles.add(ChatBubble(id = asstId, role = "assistant", content = "", streaming = true, timestamp = nowIso()))
        streamingId = asstId
        generating = true
        input = ""
        status = ChatStatus.Sending
        println("[chat] send convId=$convId face=$selectedFaceId len=${text.length}")
        scope.launch {
            runCatching { webSocket.sendMessage(convId, text, selectedFaceId, locale) }
                .onSuccess { println("[chat] send OK") }
                .onFailure {
                    println("[chat] send FAILED: ${it.message}")
                    errorBanner = "Send failed: ${it.message}"
                    // Drop the empty assistant placeholder we optimistically appended.
                    removeEmptyAssistantPlaceholder(bubbles, streamingId)
                    generating = false
                    streamingId = null
                }
        }
    }

    // ---- project actions (server-side) ----

    // Create a new project from the dialog draft, then refresh the project list.
    fun createProject(draft: ProjectDraft) {
        val name = draft.name.trim()
        if (name.isEmpty()) return
        scope.launch {
            runCatching {
                restClient.createProject(
                    name = name,
                    description = draft.description.trim().ifEmpty { null },
                    instructions = draft.instructions.trim().ifEmpty { null },
                    defaultFaceId = draft.defaultFaceId,
                    defaultBackend = draft.defaultBackend,
                )
            }
                .onSuccess { refreshProjects() }
                .onFailure { errorBanner = "Create project failed: ${it.message}" }
        }
    }

    // Save edits: send the FULL project (all five fields) so unchanged fields persist server-side.
    fun saveProject(draft: EditProjectDraft) {
        val name = draft.name.trim()
        if (name.isEmpty()) return
        scope.launch {
            runCatching {
                restClient.updateProject(
                    id = draft.id,
                    name = name,
                    description = draft.description.trim().ifEmpty { null },
                    instructions = draft.instructions.trim().ifEmpty { null },
                    defaultFaceId = draft.defaultFaceId,
                    defaultBackend = draft.defaultBackend,
                )
            }
                .onSuccess { refreshProjects() }
                .onFailure { errorBanner = "Save project failed: ${it.message}" }
        }
    }

    // Delete (archive) a project: the gateway unassigns member conversations → refresh both lists.
    fun deleteProject(project: ProjectSummary) {
        scope.launch {
            runCatching { restClient.deleteProject(project.id) }
                .onSuccess {
                    collapsedProjectIds.remove(project.id)
                    refreshConversations() // also refreshes projects
                }
                .onFailure { errorBanner = "Delete project failed: ${it.message}" }
        }
    }

    // Open the Edit dialog: fetch the FULL project (incl. instructions) to prefill all fields.
    fun openEditProject(project: ProjectSummary) {
        scope.launch {
            runCatching { restClient.getProject(project.id) }
                .onSuccess { full ->
                    editProjectDraft = EditProjectDraft(
                        id = full.id,
                        name = full.name,
                        description = full.description.orEmpty(),
                        instructions = full.instructions.orEmpty(),
                        defaultFaceId = full.defaultFaceId,
                        defaultBackend = full.defaultBackend,
                    )
                }
                .onFailure { errorBanner = "Failed to load project: ${it.message}" }
        }
    }

    // Move a conversation into a project (or Unfiled when projectId == null), then refresh.
    fun moveConversation(convId: String, projectId: String?) {
        scope.launch {
            runCatching { restClient.assignConversationToProject(convId, projectId) }
                .onSuccess { refreshConversations() }
                .onFailure { errorBanner = "Move failed: ${it.message}" }
        }
    }

    // Create a conversation inside a project and open it (inherits the project's face/backend server-side).
    fun newChatInProject(projectId: String) {
        if (generating) return
        scope.launch {
            runCatching { restClient.createConversation(title = "New Conversation", faceId = null, projectId = projectId) }
                .onSuccess { conv ->
                    conversations.add(0, conv)
                    conversationId = conv.id
                    // Project conversations inherit the project's backend server-side; reflect it.
                    selectedBackend = conv.backendPreference
                    streamingId = null
                    generating = false
                    bubbles.clear()
                    status = ChatStatus.Connected
                    refreshConversations()
                }
                .onFailure { errorBanner = "Create conversation failed: ${it.message}" }
        }
    }

    GradientBackground {
        Row(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            // ---- conversation sidebar (collapsible) ----
            if (!sidebarCollapsed) {
            ConversationSidebar(
                conversations = conversations,
                activeId = conversationId,
                generating = generating,
                projects = projects,
                collapsedProjectIds = collapsedProjectIds,
                onNewChat = { newChat() },
                onNewProject = { createProjectDraft = ProjectDraft() },
                onToggleCollapse = { pid ->
                    if (collapsedProjectIds.contains(pid)) collapsedProjectIds.remove(pid)
                    else collapsedProjectIds.add(pid)
                },
                onSelect = { switchTo(it) },
                onRename = { conv ->
                    renameText = conv.title ?: conv.lastMessagePreview ?: ""
                    renameTarget = conv
                },
                onArchive = { archive(it) },
                onMove = { conv, projectId -> moveConversation(conv.id, projectId) },
                onNewChatInProject = { project -> newChatInProject(project.id) },
                onProjectSettings = { project -> openEditProject(project) },
                onDeleteProject = { deleteProject(it) },
                modifier = Modifier.width(260.dp).fillMaxHeight(),
            )

            Spacer(Modifier.width(16.dp))
            }

            // ---- chat column ----
            Column(modifier = Modifier.weight(1.4f).fillMaxHeight()) {
                // top bar: face picker + status + nav
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    PillChip(
                        // guillemets (not emoji): » reveal the sidebar, « collapse it
                        text = if (sidebarCollapsed) "»" else "«",
                        onClick = { sidebarCollapsed = !sidebarCollapsed },
                    )
                    Spacer(Modifier.width(8.dp))
                    if (canvasHtml != null || canvasUrl != null) {
                        PillChip(
                            text = if (canvasOpen) stringResource(Res.string.chat_canvas_open) else stringResource(Res.string.chat_canvas),
                            onClick = { canvasOpen = !canvasOpen },
                            active = canvasOpen,
                        )
                        Spacer(Modifier.width(8.dp))
                    }
                    // U9 face selector: Simple = plain mode picker (data-driven off simple_slot);
                    // Pro = the pre-U9 face pill + dropdown, verbatim. The mode picker only takes over
                    // once the live faces expose simple_slot faces (else the pill is the fallback).
                    val simpleModeRows =
                        if (useModePicker(experienceLevel)) simpleModes(faces) else emptyList()
                    // Power chips (backend + profile) are inline in Pro; in Simple they hide behind
                    // "Details". Pro: always true → the Pro chip cluster stays byte-identical to pre-U9.
                    val powerVisible = showPowerChipsInline(experienceLevel) || powerExpanded
                    if (simpleModeRows.isNotEmpty()) {
                        SimpleModeSelector(
                            modes = simpleModeRows,
                            selectedFaceId = selectedFaceId,
                            onPick = { selectedFaceId = it },
                        )
                    } else {
                    Box {
                        val label = faces.firstOrNull { it.id == selectedFaceId }?.name ?: selectedFaceId
                        PillChip(
                            text = stringResource(Res.string.chat_face_pill, label),
                            onClick = { faceMenuOpen = true },
                            active = true,
                        )
                        DropdownMenu(
                            expanded = faceMenuOpen,
                            onDismissRequest = { faceMenuOpen = false },
                            modifier = Modifier
                                .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.control)
                                .border(1.dp, LocalBoBClawColors.borderCard, BoBClawShapes.control),
                        ) {
                            if (faces.isEmpty()) {
                                DropdownMenuItem(text = { MenuLabel(selectedFaceId) }, onClick = { faceMenuOpen = false })
                            }
                            faces.forEach { face ->
                                DropdownMenuItem(
                                    text = { MenuLabel(face.name, active = face.id == selectedFaceId) },
                                    onClick = {
                                        selectedFaceId = face.id
                                        faceMenuOpen = false
                                    },
                                )
                            }
                        }
                    }
                    }
                    // Model/backend picker — pins a model for this conversation, or "Auto" (clears
                    // the pin → face routing), mirroring the Face picker. W2: rows are labeled with
                    // the FRIENDLY model name from the live GET /capabilities registry (e.g. "Opus
                    // 4.8" for claude_code) + a backend·availability caption, so "hit Opus" is a
                    // first-class choice; they degrade to the bare backend id until the registry
                    // loads. Selecting a row pins its backend over the UNCHANGED switchModel /
                    // backendPreference path (backend ↔ model is 1:1 in the registry).
                    // U9: inline in Pro; in Simple hidden behind "Details" until [powerVisible].
                    if (powerVisible) {
                    Spacer(Modifier.width(8.dp))
                    Box {
                        val modelOptions = buildModelPickerOptions(
                            liveBackends = capabilities?.backends ?: emptyList(),
                            staticBackends = PROJECT_BACKENDS,
                            selectedBackend = selectedBackend,
                            autoLabel = stringResource(Res.string.chat_backend_auto),
                            availableLabel = stringResource(Res.string.settings_models_available),
                            unavailableLabel = stringResource(Res.string.settings_models_unavailable),
                        )
                        // The chip shows the pinned model's friendly name (or "Auto" when unpinned).
                        val chipValue = chatBackendChipLabel(
                            modelOptions, stringResource(Res.string.chat_backend_auto),
                        )
                        PillChip(
                            text = stringResource(Res.string.chat_backend_pill, chipValue),
                            onClick = { backendMenuOpen = true },
                            active = selectedBackend != null,
                        )
                        DropdownMenu(
                            expanded = backendMenuOpen,
                            onDismissRequest = { backendMenuOpen = false },
                            modifier = Modifier
                                .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.control)
                                .border(1.dp, LocalBoBClawColors.borderCard, BoBClawShapes.control),
                        ) {
                            // Pin a backend (null = Auto ⇒ empty backend = clear → face routing).
                            // Same wire as before: model stays "" (backend ↔ model is 1:1).
                            fun applyBackend(backend: String?) {
                                selectedBackend = backend
                                backendMenuOpen = false
                                val convId = conversationId
                                if (convId != null) {
                                    // MS9-W4 (fix D): mirror the pin onto the LOCAL conversation row so a
                                    // later switchTo(convId)/reseed keeps it — otherwise the chip reverts
                                    // to "Auto" from the stale (pre-pin) backendPreference even though the
                                    // server pin persisted (routing still changed). Server write is async.
                                    val idx = conversations.indexOfFirst { it.id == convId }
                                    if (idx >= 0) {
                                        conversations[idx] = conversations[idx].copy(backendPreference = backend)
                                    }
                                    scope.launch {
                                        runCatching { webSocket.switchModel(convId, "", backend ?: "") }
                                            .onFailure { errorBanner = "Backend switch failed: ${it.message}" }
                                    }
                                }
                            }
                            modelOptions.forEach { option ->
                                DropdownMenuItem(
                                    text = { ModelMenuLabel(option) },
                                    onClick = { applyBackend(option.backendId) },
                                )
                            }
                        }
                    }
                    }
                    Spacer(Modifier.width(8.dp))
                    // Locale toggle (i18n) — cycles EN -> 简 -> 繁; flips BOTH the UI catalog
                    // (Compose Resources, via prefs.locale re-key) AND the response language
                    // (S0 directive, via switch_locale on the WS session).
                    PillChip(
                        text = when (locale) { "zh-Hans" -> "简"; "zh-Hant" -> "繁"; else -> "EN" },
                        onClick = {
                            // Single source of truth: flip prefs.locale (the UI re-keys via Compose
                            // Resources). Responses follow via the per-message `locale` arg on
                            // sendMessage (S0) — the gateway prefers payload.locale over the session
                            // pin — so no async switch_locale round-trip that could desync/race/fail.
                            onSetLocale(nextLocale(locale))
                        },
                        active = locale != "en",
                    )
                    // Profile picker (HOW layer) — pin a saved profile (e.g. a council) to
                    // this conversation, or "Off" to clear it. Applies via switch_profile.
                    // U9: inline in Pro; in Simple hidden behind "Details" until [powerVisible].
                    if (powerVisible) {
                    Spacer(Modifier.width(8.dp))
                    Box {
                        PillChip(
                            text = stringResource(Res.string.chat_profile_pill, selectedProfile ?: stringResource(Res.string.chat_profile_off)),
                            onClick = { profileMenuOpen = true },
                            active = selectedProfile != null,
                        )
                        DropdownMenu(
                            expanded = profileMenuOpen,
                            onDismissRequest = { profileMenuOpen = false },
                            modifier = Modifier
                                .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.control)
                                .border(1.dp, LocalBoBClawColors.borderCard, BoBClawShapes.control),
                        ) {
                            fun applyProfile(name: String?) {
                                selectedProfile = name
                                profileMenuOpen = false
                                val convId = conversationId
                                if (convId != null) {
                                    scope.launch {
                                        runCatching { webSocket.switchProfile(convId, name ?: "") }
                                            .onFailure { errorBanner = "Profile switch failed: ${it.message}" }
                                    }
                                }
                            }
                            DropdownMenuItem(
                                text = { MenuLabel(stringResource(Res.string.chat_off), active = selectedProfile == null, mono = true) },
                                onClick = { applyProfile(null) },
                            )
                            profileNames.forEach { name ->
                                DropdownMenuItem(
                                    text = { MenuLabel(name, active = selectedProfile == name, mono = true) },
                                    onClick = { applyProfile(name) },
                                )
                            }
                        }
                    }
                    }
                    // U9 "Details" affordance (Simple only): reveals the collapsed power chips
                    // (backend + profile) on demand. Absent entirely in Pro (byte-identical surface).
                    if (!showPowerChipsInline(experienceLevel)) {
                        Spacer(Modifier.width(8.dp))
                        PillChip(
                            text = if (powerExpanded) stringResource(Res.string.chat_details_hide)
                                   else stringResource(Res.string.chat_details),
                            onClick = { powerExpanded = !powerExpanded },
                            active = powerExpanded,
                        )
                    }
                    Spacer(Modifier.width(12.dp))
                    Text(
                        status.label(),
                        color = LocalBoBClawColors.textSecondary,
                        style = BoBClawType.monoLabel,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                    )
                    Box {
                        PillChip(
                            text = "⋮",
                            onClick = { menuOpen = true },
                        )
                        DropdownMenu(
                            expanded = menuOpen,
                            onDismissRequest = { menuOpen = false },
                            modifier = Modifier
                                .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.control)
                                .border(1.dp, LocalBoBClawColors.borderCard, BoBClawShapes.control),
                        ) {
                            if (onOpenDashboard != null) {
                                DropdownMenuItem(
                                    text = { MenuLabel(stringResource(Res.string.chat_dashboard)) },
                                    onClick = { menuOpen = false; onOpenDashboard() },
                                )
                            }
                            DropdownMenuItem(
                                text = { MenuLabel(stringResource(Res.string.chat_log_out)) },
                                onClick = {
                                    menuOpen = false
                                    webSocket.disconnect()
                                    authManager.logout()
                                    onLogout()
                                },
                            )
                        }
                    }
                }

                if (errorBanner != null) {
                    Spacer(Modifier.height(8.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth()
                            .background(LocalBoBClawColors.alert.copy(alpha = 0.14f), BoBClawShapes.control)
                            .border(1.dp, LocalBoBClawColors.alert.copy(alpha = 0.5f), BoBClawShapes.control)
                            .padding(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            errorBanner ?: "",
                            color = LocalBoBClawColors.alert,
                            style = BoBClawType.label,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            stringResource(Res.string.chat_dismiss),
                            color = LocalBoBClawColors.accent,
                            style = BoBClawType.label,
                            modifier = Modifier.clickable { errorBanner = null },
                        )
                    }
                }

                Spacer(Modifier.height(12.dp))

                // message list
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxWidth().weight(1f).glassMorphism().padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(bubbles, key = { it.id }) { bubble ->
                        MessageRow(bubble, voiceBeta = voiceBeta)
                        if (bubble.role == "assistant") {
                            val inlineHtml = extractHtmlArtifact(bubble.content)
                            val filePath = extractFileArtifact(bubble.content)
                            when {
                                inlineHtml != null -> TextButton(onClick = {
                                    canvasHtml = inlineHtml; canvasUrl = null; canvasOpen = true
                                }) { Text(stringResource(Res.string.chat_open_in_canvas), color = LocalBoBClawColors.accent, style = BoBClawType.label) }
                                filePath != null -> TextButton(onClick = {
                                    canvasUrl = "file:///" + filePath.replace('\\', '/'); canvasHtml = null; canvasOpen = true
                                }) { Text(stringResource(Res.string.chat_open_file_in_canvas), color = LocalBoBClawColors.accent, style = BoBClawType.label) }
                            }
                        }
                    }
                }

                Spacer(Modifier.height(12.dp))

                ComposerBar(
                    input = input,
                    onInputChange = { input = it },
                    enabled = conversationId != null,
                    generating = generating,
                    capabilities = capabilities,
                    onSend = { send() },
                    onStop = { scope.launch { runCatching { webSocket.stopGeneration() } } },
                    onPickFace = { id -> selectedFaceId = id },
                    onPickBackend = { name ->
                        selectedBackend = name
                        val convId = conversationId
                        if (convId != null) {
                            scope.launch {
                                runCatching { webSocket.switchModel(convId, "", name) }
                                    .onFailure { errorBanner = "Backend switch failed: ${it.message}" }
                            }
                        }
                    },
                    onRunInit = { send(INIT_PROMPT) },
                    experienceLevel = experienceLevel,
                    voiceBeta = voiceBeta,
                )
            }

            // ---- canvas pane (collapsible right) ----
            if (canvasOpen && (canvasHtml != null || canvasUrl != null)) {
                Spacer(Modifier.width(16.dp))
                ArtifactPanel(
                    canvasHtml = canvasHtml,
                    canvasUrl = canvasUrl,
                    canvasUrlInput = canvasUrlInput,
                    onUrlInputChange = { canvasUrlInput = it },
                    onGo = { goToCanvasUrl(canvasUrlInput) },
                    onClear = { canvasUrlInput = ""; canvasUrl = null; canvasHtml = null },
                    onClose = { canvasOpen = false },
                    artifactRenderer = artifactRenderer,
                )
            }
        }
    }

    // ---- rename dialog ----
    val target = renameTarget
    if (target != null) {
        AlertDialog(
            onDismissRequest = { renameTarget = null },
            title = { Text(stringResource(Res.string.chat_rename_conversation), color = BoBClawColors.TextPrimary) },
            text = {
                OutlinedTextField(
                    value = renameText,
                    onValueChange = { renameText = it },
                    singleLine = true,
                    placeholder = { Text(stringResource(Res.string.chat_title)) },
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BoBClawColors.TextPrimary,
                        unfocusedTextColor = BoBClawColors.TextPrimary,
                        focusedBorderColor = BoBClawColors.AccentGreen,
                        unfocusedBorderColor = BoBClawColors.BorderSubtle,
                        cursorColor = BoBClawColors.AccentGreen,
                    ),
                )
            },
            confirmButton = {
                TextButton(
                    enabled = renameText.isNotBlank(),
                    onClick = {
                        rename(target, renameText)
                        renameTarget = null
                    },
                ) { Text(stringResource(Res.string.chat_rename), color = BoBClawColors.AccentGreen) }
            },
            dismissButton = {
                TextButton(onClick = { renameTarget = null }) {
                    Text(stringResource(Res.string.chat_cancel), color = BoBClawColors.TextSecondary)
                }
            },
            containerColor = BoBClawColors.GradientBottom,
        )
    }

    // ---- create project dialog ----
    val createDraft = createProjectDraft
    if (createDraft != null) {
        ProjectDialog(
            title = stringResource(Res.string.chat_new_project),
            confirmLabel = stringResource(Res.string.chat_create),
            draft = createDraft,
            faces = faces,
            onDraftChange = { createProjectDraft = it },
            onDismiss = { createProjectDraft = null },
            onConfirm = {
                createProject(createDraft)
                createProjectDraft = null
            },
        )
    }

    // ---- edit project dialog (prefilled from the full project via getProject) ----
    val editDraft = editProjectDraft
    if (editDraft != null) {
        ProjectDialog(
            title = stringResource(Res.string.chat_project_settings),
            confirmLabel = stringResource(Res.string.chat_save),
            draft = editDraft.toDraft(),
            faces = faces,
            onDraftChange = { editProjectDraft = editDraft.withDraft(it) },
            onDismiss = { editProjectDraft = null },
            onConfirm = {
                saveProject(editDraft)
                editProjectDraft = null
            },
        )
    }
}

/**
 * U9 Simple-mode picker (SPEC §6): a row of plain-language mode pills (e.g. Quick / Think hard /
 * Team of experts), built ENTIRELY from [SimpleMode] rows derived off `Face.simpleSlot` — there is
 * no hardcoded app-side faceId→mode map. Picking a mode applies its `faceId` as the chat's
 * `selectedFaceId` pin — the SAME mechanism the Pro face dropdown uses (presentation only, no routing
 * change). The active pill is whichever mode's faceId is currently pinned.
 */
@Composable
private fun SimpleModeSelector(
    modes: List<SimpleMode>,
    selectedFaceId: String,
    onPick: (String) -> Unit,
) {
    Row(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        modes.forEach { mode ->
            PillChip(
                text = mode.label,
                onClick = { onPick(mode.faceId) },
                active = mode.faceId == selectedFaceId,
            )
        }
    }
}
