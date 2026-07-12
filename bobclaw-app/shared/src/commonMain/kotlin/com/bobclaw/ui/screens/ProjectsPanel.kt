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

// MS8-A2 decomposition: project create/edit dialogs + backend/face pickers, split out of ChatScreen.kt.
// Sentinel id for the synthetic "Unfiled" group (conversations with no/dangling assignment).
internal const val UNFILED_ID = "__unfiled__"

// "Auto (none)" sentinel for the face/backend dropdowns (maps to a null wire value).
internal const val AUTO_NONE = "Auto (none)"

// Fixed backend choices for the project default-backend dropdown (bare strings, server contract).
internal val PROJECT_BACKENDS = listOf(
    "deepseek_v4_flash", "claude_code", "minimax", "kimi_code", "gemini_flash", "local",
)

// Editable field bundle for the Create Project dialog.
internal data class ProjectDraft(
    val name: String = "",
    val description: String = "",
    val instructions: String = "",
    val defaultFaceId: String? = null,
    val defaultBackend: String? = null,
)

// Edit-dialog draft: carries the project id alongside the editable fields.
internal data class EditProjectDraft(
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
internal fun ProjectDialog(
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
internal fun ProjectDropdown(
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
