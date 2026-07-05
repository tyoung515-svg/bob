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
import com.bobclaw.model.Conversation
import com.bobclaw.model.Face
import com.bobclaw.model.ProjectSummary
import com.bobclaw.model.ServerMessage
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.RestClient
import com.bobclaw.ui.MarkdownText
import com.bobclaw.ui.extractFileArtifact
import com.bobclaw.ui.extractHtmlArtifact
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import com.bobclaw.ui.theme.glassMorphism
import kotlinx.coroutines.launch

/** Local chat-bubble model (decoupled from the persisted Message wire type). */
private data class ChatBubble(
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
) {
    val scope = rememberCoroutineScope()

    val bubbles = remember { mutableStateListOf<ChatBubble>() }
    var faces by remember { mutableStateOf<List<Face>>(emptyList()) }
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

    fun send() {
        val text = input.trim()
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
                        text = if (sidebarCollapsed) "☰" else "«",
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
                    Spacer(Modifier.width(8.dp))
                    // Backend picker — lets the user pin a backend or pick "Auto" (clears the pin),
                    // mirroring the Face picker. Applies live to the active conversation via switchModel.
                    Box {
                        PillChip(
                            text = stringResource(Res.string.chat_backend_pill, selectedBackend ?: stringResource(Res.string.chat_backend_auto)),
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
                            // Pick a backend, or "Auto" to clear the pin (empty backend = clear → face routing).
                            fun applyBackend(backend: String?) {
                                selectedBackend = backend
                                backendMenuOpen = false
                                val convId = conversationId
                                if (convId != null) {
                                    scope.launch {
                                        runCatching { webSocket.switchModel(convId, "", backend ?: "") }
                                            .onFailure { errorBanner = "Backend switch failed: ${it.message}" }
                                    }
                                }
                            }
                            DropdownMenuItem(
                                text = { MenuLabel(stringResource(Res.string.chat_auto), active = selectedBackend == null, mono = true) },
                                onClick = { applyBackend(null) },
                            )
                            PROJECT_BACKENDS.forEach { backend ->
                                DropdownMenuItem(
                                    text = { MenuLabel(backend, active = selectedBackend == backend, mono = true) },
                                    onClick = { applyBackend(backend) },
                                )
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
                    Spacer(Modifier.width(8.dp))
                    // Profile picker (HOW layer) — pin a saved profile (e.g. a council) to
                    // this conversation, or "Off" to clear it. Applies via switch_profile.
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
                        MessageRow(bubble)
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

                // input row
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(
                        value = input,
                        onValueChange = { input = it },
                        placeholder = { Text(stringResource(Res.string.chat_message_placeholder), color = LocalBoBClawColors.textMuted) },
                        enabled = conversationId != null,
                        textStyle = BoBClawType.body,
                        shape = BoBClawShapes.control,
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                        keyboardActions = KeyboardActions(onSend = { send() }),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedContainerColor = LocalBoBClawColors.surfaceCard,
                            unfocusedContainerColor = LocalBoBClawColors.surfaceCard,
                            disabledContainerColor = LocalBoBClawColors.surfaceCard,
                            focusedTextColor = LocalBoBClawColors.textBody,
                            unfocusedTextColor = LocalBoBClawColors.textBody,
                            focusedBorderColor = LocalBoBClawColors.accent,
                            unfocusedBorderColor = LocalBoBClawColors.borderControl,
                            cursorColor = LocalBoBClawColors.accent,
                        ),
                        // Enter sends; Shift+Enter inserts a newline. (imeAction doesn't catch a
                        // hardware Enter on desktop, so intercept the key event directly.)
                        modifier = Modifier
                            .weight(1f)
                            .onPreviewKeyEvent { ev ->
                                if (ev.type == KeyEventType.KeyDown && ev.key == Key.Enter && !ev.isShiftPressed) {
                                    send()
                                    true
                                } else {
                                    false
                                }
                            },
                    )
                    Spacer(Modifier.width(8.dp))
                    if (generating) {
                        OutlinedButton(
                            onClick = { scope.launch { runCatching { webSocket.stopGeneration() } } },
                            shape = BoBClawShapes.control,
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = LocalBoBClawColors.alert),
                        ) { Text(stringResource(Res.string.chat_stop), style = BoBClawType.label) }
                    } else {
                        Button(
                            onClick = { send() },
                            enabled = conversationId != null && input.isNotBlank(),
                            shape = BoBClawShapes.control,
                            colors = ButtonDefaults.buttonColors(
                                containerColor = LocalBoBClawColors.accent,
                                contentColor = LocalBoBClawColors.onAccent,
                                disabledContainerColor = LocalBoBClawColors.surfaceCard,
                                disabledContentColor = LocalBoBClawColors.textMuted,
                            ),
                        ) { Text(stringResource(Res.string.chat_send), style = BoBClawType.label) }
                    }
                }
            }

            // ---- canvas pane (collapsible right) ----
            if (canvasOpen && (canvasHtml != null || canvasUrl != null)) {
                Spacer(Modifier.width(16.dp))
                Column(modifier = Modifier.weight(1f).fillMaxHeight()) {
                    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            stringResource(Res.string.chat_canvas),
                            color = BoBClawColors.TextPrimary,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.weight(1f),
                        )
                        OutlinedButton(
                            onClick = { canvasOpen = false },
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = BoBClawColors.TextSecondary),
                        ) { Text("✕") }
                    }
                    Spacer(Modifier.height(8.dp))
                    // URL bar — turns the canvas into a mini-browser. Enter or "Go" navigates;
                    // "Clear" blanks it. Mirrors the chat input's onPreviewKeyEvent Enter handling.
                    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(
                            value = canvasUrlInput,
                            onValueChange = { canvasUrlInput = it },
                            singleLine = true,
                            placeholder = { Text(stringResource(Res.string.chat_enter_url)) },
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Go),
                            keyboardActions = KeyboardActions(onGo = { goToCanvasUrl(canvasUrlInput) }),
                            colors = OutlinedTextFieldDefaults.colors(
                                focusedTextColor = BoBClawColors.TextPrimary,
                                unfocusedTextColor = BoBClawColors.TextPrimary,
                                focusedBorderColor = BoBClawColors.AccentGreen,
                                unfocusedBorderColor = BoBClawColors.BorderSubtle,
                                cursorColor = BoBClawColors.AccentGreen,
                            ),
                            modifier = Modifier
                                .weight(1f)
                                .onPreviewKeyEvent { ev ->
                                    if (ev.type == KeyEventType.KeyDown && ev.key == Key.Enter && !ev.isShiftPressed) {
                                        goToCanvasUrl(canvasUrlInput)
                                        true
                                    } else {
                                        false
                                    }
                                },
                        )
                        Spacer(Modifier.width(8.dp))
                        Button(
                            onClick = { goToCanvasUrl(canvasUrlInput) },
                            enabled = canvasUrlInput.isNotBlank(),
                            colors = ButtonDefaults.buttonColors(containerColor = BoBClawColors.AccentGreen),
                        ) { Text(stringResource(Res.string.chat_go)) }
                        Spacer(Modifier.width(8.dp))
                        OutlinedButton(
                            onClick = {
                                canvasUrlInput = ""
                                canvasUrl = null
                                canvasHtml = null
                            },
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = BoBClawColors.TextSecondary),
                        ) { Text(stringResource(Res.string.chat_clear)) }
                    }
                    Spacer(Modifier.height(8.dp))
                    Box(modifier = Modifier.fillMaxSize().glassMorphism()) {
                        artifactRenderer(canvasHtml, canvasUrl, Modifier.fillMaxSize())
                    }
                }
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

// Sentinel id for the synthetic "Unfiled" group (conversations with no/dangling assignment).
private const val UNFILED_ID = "__unfiled__"

// "Auto (none)" sentinel for the face/backend dropdowns (maps to a null wire value).
private const val AUTO_NONE = "Auto (none)"

// Fixed backend choices for the project default-backend dropdown (bare strings, server contract).
private val PROJECT_BACKENDS = listOf(
    "deepseek_v4_flash", "claude_code", "minimax", "kimi_code", "gemini_flash", "local",
)

// Editable field bundle for the Create Project dialog.
private data class ProjectDraft(
    val name: String = "",
    val description: String = "",
    val instructions: String = "",
    val defaultFaceId: String? = null,
    val defaultBackend: String? = null,
)

// Edit-dialog draft: carries the project id alongside the editable fields.
private data class EditProjectDraft(
    val id: String,
    val name: String,
    val description: String,
    val instructions: String,
    val defaultFaceId: String?,
    val defaultBackend: String?,
) {
    fun toDraft(): ProjectDraft = ProjectDraft(name, description, instructions, defaultFaceId, defaultBackend)
    fun withDraft(d: ProjectDraft): EditProjectDraft =
        copy(name = d.name, description = d.description, instructions = d.instructions,
            defaultFaceId = d.defaultFaceId, defaultBackend = d.defaultBackend)
}

// Shared create/edit project dialog. Name (required), Description (single line), Instructions
// (multiline), Default face + Default backend dropdowns (each with an "Auto (none)" → null option).
@Composable
private fun ProjectDialog(
    title: String,
    confirmLabel: String,
    draft: ProjectDraft,
    faces: List<Face>,
    onDraftChange: (ProjectDraft) -> Unit,
    onDismiss: () -> Unit,
    onConfirm: () -> Unit,
) {
    val fieldColors = OutlinedTextFieldDefaults.colors(
        focusedTextColor = BoBClawColors.TextPrimary,
        unfocusedTextColor = BoBClawColors.TextPrimary,
        focusedBorderColor = BoBClawColors.AccentGreen,
        unfocusedBorderColor = BoBClawColors.BorderSubtle,
        cursorColor = BoBClawColors.AccentGreen,
    )
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title, color = BoBClawColors.TextPrimary) },
        text = {
            Column(modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState())) {
                OutlinedTextField(
                    value = draft.name,
                    onValueChange = { onDraftChange(draft.copy(name = it)) },
                    singleLine = true,
                    placeholder = { Text(stringResource(Res.string.chat_project_name)) },
                    label = { Text(stringResource(Res.string.chat_name)) },
                    colors = fieldColors,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = draft.description,
                    onValueChange = { onDraftChange(draft.copy(description = it)) },
                    singleLine = true,
                    placeholder = { Text(stringResource(Res.string.chat_short_description)) },
                    label = { Text(stringResource(Res.string.chat_description)) },
                    colors = fieldColors,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = draft.instructions,
                    onValueChange = { onDraftChange(draft.copy(instructions = it)) },
                    placeholder = { Text(stringResource(Res.string.chat_applied_to_every_conversation_in_this_project)) },
                    label = { Text(stringResource(Res.string.chat_project_context_instructions)) },
                    colors = fieldColors,
                    modifier = Modifier.fillMaxWidth().height(120.dp),
                )
                Spacer(Modifier.height(8.dp))
                // Default face dropdown ("Auto (none)" → null).
                val faceLabel = draft.defaultFaceId
                    ?.let { id -> faces.firstOrNull { it.id == id }?.name ?: id }
                    ?: AUTO_NONE
                ProjectDropdown(
                    label = stringResource(Res.string.chat_default_face),
                    current = faceLabel,
                ) { dismiss ->
                    DropdownMenuItem(
                        text = { Text(AUTO_NONE) },
                        onClick = { onDraftChange(draft.copy(defaultFaceId = null)); dismiss() },
                    )
                    faces.forEach { face ->
                        DropdownMenuItem(
                            text = { Text(face.name) },
                            onClick = { onDraftChange(draft.copy(defaultFaceId = face.id)); dismiss() },
                        )
                    }
                }
                Spacer(Modifier.height(8.dp))
                // Default backend dropdown ("Auto (none)" → null + a fixed set).
                ProjectDropdown(
                    label = stringResource(Res.string.chat_default_backend),
                    current = draft.defaultBackend ?: AUTO_NONE,
                ) { dismiss ->
                    DropdownMenuItem(
                        text = { Text(AUTO_NONE) },
                        onClick = { onDraftChange(draft.copy(defaultBackend = null)); dismiss() },
                    )
                    PROJECT_BACKENDS.forEach { backend ->
                        DropdownMenuItem(
                            text = { Text(backend) },
                            onClick = { onDraftChange(draft.copy(defaultBackend = backend)); dismiss() },
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = draft.name.isNotBlank(),
                onClick = onConfirm,
            ) { Text(confirmLabel, color = BoBClawColors.AccentGreen) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(Res.string.chat_cancel), color = BoBClawColors.TextSecondary)
            }
        },
        containerColor = BoBClawColors.GradientBottom,
    )
}

// A labelled dropdown rendered as a bordered, clickable row (OutlinedTextField is read-only on
// desktop, so we hand-roll the trigger to match the dialog's look). [items] is given a dismiss fn.
@Composable
private fun ProjectDropdown(
    label: String,
    current: String,
    items: @Composable (dismiss: () -> Unit) -> Unit,
) {
    var open by remember { mutableStateOf(false) }
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(label, color = BoBClawColors.TextSecondary, fontSize = 11.sp)
        Spacer(Modifier.height(2.dp))
        Box {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(BoBClawColors.ZoneHeaderBg, RoundedCornerShape(8.dp))
                    .clickable { open = true }
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = current,
                    color = BoBClawColors.TextPrimary,
                    fontSize = 13.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Text("▾", color = BoBClawColors.TextSecondary, fontSize = 12.sp)
            }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                items { open = false }
            }
        }
    }
}

@Composable
private fun ConversationSidebar(
    conversations: List<Conversation>,
    activeId: String?,
    generating: Boolean,
    projects: List<ProjectSummary>,
    collapsedProjectIds: List<String>,
    onNewChat: () -> Unit,
    onNewProject: () -> Unit,
    onToggleCollapse: (String) -> Unit,
    onSelect: (String) -> Unit,
    onRename: (Conversation) -> Unit,
    onArchive: (Conversation) -> Unit,
    onMove: (Conversation, String?) -> Unit,
    onNewChatInProject: (ProjectSummary) -> Unit,
    onProjectSettings: (ProjectSummary) -> Unit,
    onDeleteProject: (ProjectSummary) -> Unit,
    modifier: Modifier = Modifier,
) {
    // Group conversations by server project. A conversation whose projectId is null OR points at a
    // project not in the list (dangling) is treated as Unfiled. Preserve the gateway's newest-first order.
    val validProjectIds = projects.mapTo(HashSet()) { it.id }
    val byProject: Map<String, List<Conversation>> = conversations.groupBy { conv ->
        val pid = conv.projectId
        if (pid != null && pid in validProjectIds) pid else UNFILED_ID
    }

    Column(modifier = modifier.glassMorphism().padding(8.dp)) {
        Button(
            onClick = onNewChat,
            enabled = !generating,
            modifier = Modifier.fillMaxWidth(),
            colors = ButtonDefaults.buttonColors(containerColor = BoBClawColors.AccentGreen),
        ) { Text(stringResource(Res.string.chat_plus_new_chat)) }

        Spacer(Modifier.height(8.dp))

        OutlinedButton(
            onClick = onNewProject,
            modifier = Modifier.fillMaxWidth(),
            colors = ButtonDefaults.outlinedButtonColors(contentColor = BoBClawColors.AccentGreen),
        ) { Text(stringResource(Res.string.chat_plus_new_project)) }

        Spacer(Modifier.height(8.dp))

        if (conversations.isEmpty() && projects.isEmpty()) {
            Text(
                stringResource(Res.string.chat_no_conversations_yet),
                color = BoBClawColors.TextSecondary,
                fontSize = 12.sp,
                modifier = Modifier.padding(8.dp),
            )
        } else {
            val unfiled = byProject[UNFILED_ID].orEmpty()
            LazyColumn(
                modifier = Modifier.fillMaxWidth().weight(1f),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                // Named projects, in server list order.
                projects.forEach { project ->
                    val convs = byProject[project.id].orEmpty()
                    val collapsed = collapsedProjectIds.contains(project.id)
                    item(key = "project-${project.id}") {
                        ProjectHeaderRow(
                            name = project.name,
                            count = convs.size,
                            collapsed = collapsed,
                            onToggle = { onToggleCollapse(project.id) },
                            onNewChatHere = { onNewChatInProject(project) },
                            onSettings = { onProjectSettings(project) },
                            onDelete = { onDeleteProject(project) },
                        )
                    }
                    if (!collapsed) {
                        items(convs, key = { it.id }) { conv ->
                            ConversationSidebarRow(
                                conv = conv,
                                active = conv.id == activeId,
                                generating = generating,
                                projects = projects,
                                onSelect = { onSelect(conv.id) },
                                onRename = { onRename(conv) },
                                onArchive = { onArchive(conv) },
                                onMove = { projectId -> onMove(conv, projectId) },
                            )
                        }
                    }
                }

                // Unfiled group (always last). Shown even when empty if any projects exist,
                // so it's an obvious drop target; hidden only when there are no projects at all.
                if (unfiled.isNotEmpty() || projects.isNotEmpty()) {
                    val collapsed = collapsedProjectIds.contains(UNFILED_ID)
                    item(key = "project-unfiled") {
                        ProjectHeaderRow(
                            name = stringResource(Res.string.chat_unfiled),
                            count = unfiled.size,
                            collapsed = collapsed,
                            onToggle = { onToggleCollapse(UNFILED_ID) },
                            onNewChatHere = null,
                            onSettings = null,
                            onDelete = null,
                        )
                    }
                    if (!collapsed) {
                        items(unfiled, key = { it.id }) { conv ->
                            ConversationSidebarRow(
                                conv = conv,
                                active = conv.id == activeId,
                                generating = generating,
                                projects = projects,
                                onSelect = { onSelect(conv.id) },
                                onRename = { onRename(conv) },
                                onArchive = { onArchive(conv) },
                                onMove = { projectId -> onMove(conv, projectId) },
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ProjectHeaderRow(
    name: String,
    count: Int,
    collapsed: Boolean,
    onToggle: () -> Unit,
    onNewChatHere: (() -> Unit)?,
    onSettings: (() -> Unit)?,
    onDelete: (() -> Unit)?,
) {
    var menuOpen by remember { mutableStateOf(false) }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(BoBClawColors.ZoneHeaderBg, RoundedCornerShape(8.dp))
            .clickable { onToggle() }
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // chevron: ▼ when expanded, ▶ when collapsed
        Text(
            text = if (collapsed) "▶" else "▼",
            color = BoBClawColors.TextSecondary,
            fontSize = 10.sp,
        )
        Spacer(Modifier.width(6.dp))
        Text(
            text = name,
            color = BoBClawColors.TextPrimary,
            fontSize = 12.sp,
            fontWeight = FontWeight.SemiBold,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Text(
            text = "$count",
            color = BoBClawColors.TextSecondary,
            fontSize = 11.sp,
            modifier = Modifier.padding(horizontal = 4.dp),
        )
        // Project actions menu (Unfiled passes null callbacks → no menu shown).
        if (onNewChatHere != null || onSettings != null || onDelete != null) {
            Box {
                Text(
                    text = "⋯",
                    color = BoBClawColors.TextSecondary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier
                        .clickable { menuOpen = true }
                        .padding(horizontal = 6.dp),
                )
                DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                    if (onNewChatHere != null) {
                        DropdownMenuItem(
                            text = { Text(stringResource(Res.string.chat_new_conversation_here)) },
                            onClick = {
                                menuOpen = false
                                onNewChatHere()
                            },
                        )
                    }
                    if (onSettings != null) {
                        DropdownMenuItem(
                            text = { Text(stringResource(Res.string.chat_project_settings)) },
                            onClick = {
                                menuOpen = false
                                onSettings()
                            },
                        )
                    }
                    if (onDelete != null) {
                        DropdownMenuItem(
                            text = { Text(stringResource(Res.string.chat_delete_project)) },
                            onClick = {
                                menuOpen = false
                                onDelete()
                            },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun ConversationSidebarRow(
    conv: Conversation,
    active: Boolean,
    generating: Boolean,
    projects: List<ProjectSummary>,
    onSelect: () -> Unit,
    onRename: () -> Unit,
    onArchive: () -> Unit,
    onMove: (String?) -> Unit,
) {
    var menuOpen by remember { mutableStateOf(false) }
    // Second menu for "Move to project ▸" (a flat project picker; opened from the row ⋯ menu).
    var moveMenuOpen by remember { mutableStateOf(false) }
    val title = conv.title?.takeIf { it.isNotBlank() } ?: conv.lastMessagePreview ?: stringResource(Res.string.chat_new_chat)
    // grey rows out while generating (switching is disabled in that window)
    val titleColor = if (generating && !active) BoBClawColors.TextSecondary else BoBClawColors.TextPrimary

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 8.dp)
            .background(
                if (active) BoBClawColors.AccentGreen.copy(alpha = 0.18f) else Color.Transparent,
                RoundedCornerShape(8.dp),
            )
            .clickable(enabled = !generating) { onSelect() }
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = title,
                color = titleColor,
                fontSize = 12.sp,
                fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            conv.lastMessagePreview?.takeIf { it.isNotBlank() }?.let { preview ->
                Text(
                    text = preview,
                    color = BoBClawColors.TextSecondary,
                    fontSize = 10.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        Box {
            Text(
                text = "⋯",
                color = BoBClawColors.TextSecondary,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold,
                modifier = Modifier
                    .clickable { menuOpen = true }
                    .padding(horizontal = 6.dp),
            )
            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                DropdownMenuItem(
                    text = { Text(stringResource(Res.string.chat_rename)) },
                    onClick = {
                        menuOpen = false
                        onRename()
                    },
                )
                DropdownMenuItem(
                    text = { Text(stringResource(Res.string.chat_move_to_project)) },
                    onClick = {
                        menuOpen = false
                        moveMenuOpen = true
                    },
                )
                DropdownMenuItem(
                    text = { Text(stringResource(Res.string.chat_archive)) },
                    onClick = {
                        menuOpen = false
                        onArchive()
                    },
                )
            }
            // "Move to project" picker: every project + Unfiled.
            DropdownMenu(expanded = moveMenuOpen, onDismissRequest = { moveMenuOpen = false }) {
                projects.forEach { project ->
                    DropdownMenuItem(
                        text = { Text(project.name) },
                        onClick = {
                            moveMenuOpen = false
                            onMove(project.id)
                        },
                    )
                }
                DropdownMenuItem(
                    text = { Text(stringResource(Res.string.chat_unfiled_menu)) },
                    onClick = {
                        moveMenuOpen = false
                        onMove(null)
                    },
                )
            }
        }
    }
}

/**
 * A themed pill-chip trigger (DESIGN §6.1 / §3.6): `surfaceCard` fill + `borderControl` hairline,
 * 20px pill radius, `accent` text when [active] (a pinned/owned value) else `textSecondary`.
 * Used for the top-bar face/backend/nav triggers — the DropdownMenu logic stays on the call site.
 */
@Composable
private fun PillChip(
    text: String,
    onClick: () -> Unit,
    active: Boolean = false,
) {
    Row(
        modifier = Modifier
            .clip(BoBClawShapes.pill)
            .background(LocalBoBClawColors.surfaceCard, BoBClawShapes.pill)
            .border(1.dp, LocalBoBClawColors.borderControl, BoBClawShapes.pill)
            .clickable { onClick() }
            .padding(horizontal = 14.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text,
            color = if (active) LocalBoBClawColors.accent else LocalBoBClawColors.textSecondary,
            style = BoBClawType.label,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** Token-colored dropdown menu item label; mono for machine values (backends), accent when selected. */
@Composable
private fun MenuLabel(text: String, active: Boolean = false, mono: Boolean = false) {
    Text(
        text = text,
        color = if (active) LocalBoBClawColors.accent else LocalBoBClawColors.textBody,
        style = if (mono) BoBClawType.monoLabel else BoBClawType.body,
    )
}

/** Current time as an ISO instant string (live message timestamps). */
private fun nowIso(): String = Clock.System.now().toString()

/** Format an ISO instant to local HH:MM. Dep-free of String.format (JVM-only on KMM common);
 *  falls back to slicing HH:MM out of the raw ISO string if parsing fails. */
private fun formatTime(iso: String): String = runCatching {
    val lt = Instant.parse(iso).toLocalDateTime(TimeZone.currentSystemDefault())
    "${lt.hour.toString().padStart(2, '0')}:${lt.minute.toString().padStart(2, '0')}"
}.getOrElse {
    val t = iso.substringAfter('T', "")
    if (t.length >= 5) t.take(5) else ""
}

@Composable
private fun MessageRow(bubble: ChatBubble) {
    val isUser = bubble.role == "user"
    val fill = if (isUser) LocalBoBClawColors.surfaceAccent else LocalBoBClawColors.surfaceCard
    val outline = if (isUser) LocalBoBClawColors.borderAccent else LocalBoBClawColors.borderCard
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start,
    ) {
        Column(
            modifier = Modifier
                .widthIn(max = 560.dp)
                .clip(BoBClawShapes.card)
                .background(fill, BoBClawShapes.card)
                .border(1.dp, outline, BoBClawShapes.card)
                .padding(12.dp),
        ) {
            val clipboard = LocalClipboardManager.current
            // Header: sender label (leading) ─── timestamp + Copy (trailing), full-width so the
            // label aligns consistently across user/assistant bubbles and the actions sit right.
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = if (isUser) stringResource(Res.string.chat_you) else stringResource(Res.string.chat_assistant),
                    color = if (isUser) LocalBoBClawColors.accentEmphasis else LocalBoBClawColors.textSecondary,
                    style = BoBClawType.label,
                )
                // subtle streaming indicator: a muted blinking caret on the live bubble
                if (bubble.streaming) {
                    Spacer(Modifier.width(6.dp))
                    Text("▌", color = LocalBoBClawColors.textMuted, style = BoBClawType.monoLabel)
                }
                Spacer(Modifier.weight(1f))
                bubble.timestamp?.let { ts ->
                    val t = formatTime(ts)
                    if (t.isNotEmpty()) {
                        Text(t, color = LocalBoBClawColors.textMuted, style = BoBClawType.monoCaption)
                    }
                }
                if (bubble.content.isNotEmpty()) {
                    Spacer(Modifier.width(10.dp))
                    Text(
                        text = stringResource(Res.string.chat_copy),
                        color = LocalBoBClawColors.textMuted,
                        style = BoBClawType.monoCaption,
                        modifier = Modifier
                            .clip(BoBClawShapes.control)
                            .clickable { clipboard.setText(AnnotatedString(bubble.content)) }
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                    )
                }
            }
            Spacer(Modifier.height(4.dp))
            when {
                // empty streaming assistant bubble: show the typing placeholder
                bubble.content.isEmpty() && bubble.streaming -> Text(
                    text = "…",
                    color = LocalBoBClawColors.textMuted,
                    style = BoBClawType.body,
                )
                // user messages stay plain (assistant replies are what we format)
                isUser -> Text(
                    text = bubble.content,
                    color = LocalBoBClawColors.textBody,
                    style = BoBClawType.body,
                )
                // assistant replies: hand-rolled markdown
                else -> MarkdownText(bubble.content, color = LocalBoBClawColors.textBody)
            }
        }
    }
}
