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
import com.bobclaw.ui.components.ReadAloudButton
import com.bobclaw.ui.components.voiceAffordancesVisible
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import com.bobclaw.ui.theme.glassMorphism

// MS8-A2 decomposition: message row renderer + shared chat chrome (PillChip/MenuLabel/time), split out of ChatScreen.kt.
/**
 * A themed pill-chip trigger (DESIGN §6.1 / §3.6): `surfaceCard` fill + `borderControl` hairline,
 * 20px pill radius, `accent` text when [active] (a pinned/owned value) else `textSecondary`.
 * Used for the top-bar face/backend/nav triggers — the DropdownMenu logic stays on the call site.
 */
@Composable
internal fun PillChip(
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
internal fun MenuLabel(text: String, active: Boolean = false, mono: Boolean = false) {
    Text(
        text = text,
        color = if (active) LocalBoBClawColors.accent else LocalBoBClawColors.textBody,
        style = if (mono) BoBClawType.monoLabel else BoBClawType.body,
    )
}

/**
 * MS9-W2 — one row in the Chat model/backend picker: the friendly model name (accent when pinned,
 * dimmed when the backend is unavailable) over an optional mono `backend · availability` caption.
 * Falls back to the bare backend id as the primary line when the live registry has no model name.
 * Pure display of a [ModelPickerOption]; the pin action stays on the call site.
 */
@Composable
internal fun ModelMenuLabel(option: ModelPickerOption) {
    Column {
        Text(
            text = option.label,
            color = when {
                option.selected -> LocalBoBClawColors.accent
                !option.available -> LocalBoBClawColors.textMuted
                else -> LocalBoBClawColors.textBody
            },
            style = BoBClawType.body,
        )
        if (option.secondary != null) {
            Text(
                text = option.secondary,
                color = LocalBoBClawColors.textMuted,
                style = BoBClawType.monoCaption,
            )
        }
    }
}

/** Current time as an ISO instant string (live message timestamps). */
internal fun nowIso(): String = Clock.System.now().toString()

/** Format an ISO instant to local HH:MM. Dep-free of String.format (JVM-only on KMM common);
 *  falls back to slicing HH:MM out of the raw ISO string if parsing fails. */
internal fun formatTime(iso: String): String = runCatching {
    val lt = Instant.parse(iso).toLocalDateTime(TimeZone.currentSystemDefault())
    "${lt.hour.toString().padStart(2, '0')}:${lt.minute.toString().padStart(2, '0')}"
}.getOrElse {
    val t = iso.substringAfter('T', "")
    if (t.length >= 5) t.take(5) else ""
}

@Composable
internal fun MessageRow(bubble: ChatBubble, voiceBeta: Boolean = false) {
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
                // U11: inert per-message "read aloud" placeholder — only when voice_beta is on and
                // there's content to read. Guarded so flag-off emits NOTHING (row byte-identical).
                if (voiceAffordancesVisible(voiceBeta) && bubble.content.isNotEmpty()) {
                    Spacer(Modifier.width(8.dp))
                    ReadAloudButton(
                        label = stringResource(Res.string.voice_read_aloud),
                        tooltip = stringResource(Res.string.placeholder_coming_soon),
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
