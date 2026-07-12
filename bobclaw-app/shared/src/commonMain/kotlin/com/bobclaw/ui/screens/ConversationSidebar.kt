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

// MS8-A2 decomposition: conversation + project sidebar, split out of ChatScreen.kt.
@Composable
internal fun ConversationSidebar(
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
internal fun ProjectHeaderRow(
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
internal fun ConversationSidebarRow(
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
