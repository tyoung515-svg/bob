package com.bobclaw.ui.screens

import com.bobclaw.shared.resources.Res
import com.bobclaw.shared.resources.chat_message_placeholder
import com.bobclaw.shared.resources.chat_message_placeholder_simple
import com.bobclaw.shared.resources.chat_send
import com.bobclaw.shared.resources.chat_stop
import com.bobclaw.shared.resources.placeholder_coming_soon
import com.bobclaw.shared.resources.voice_mic_label
import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.isShiftPressed
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.bobclaw.model.Capabilities
import com.bobclaw.ui.components.VoiceMicButton
import com.bobclaw.ui.components.voiceAffordancesVisible
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.LocalBoBClawColors

/**
 * The chat composer (MS8-A2 decomposition of `ChatScreen`): the `/` slash-palette overlay stacked
 * above the message input row + send/stop button. Enter sends (Shift+Enter newlines); typing a
 * leading `/` opens [SlashPaletteOverlay] over the LIVE capabilities registry. All state is hoisted
 * — the parent owns [input] and every action; the palette's face/backend/init pick clears the
 * composer here so the caller's pick handler only has to apply the pin.
 */
@Composable
internal fun ComposerBar(
    input: String,
    onInputChange: (String) -> Unit,
    enabled: Boolean,
    generating: Boolean,
    capabilities: Capabilities?,
    onSend: () -> Unit,
    onStop: () -> Unit,
    onPickFace: (String) -> Unit,
    onPickBackend: (String) -> Unit,
    onRunInit: () -> Unit,
    // U9 (SPEC §6): Simple shows the plain "Message Bob…" placeholder; Pro keeps today's
    // face-named placeholder (byte-identical). Default `simple` matches the pref default.
    experienceLevel: String = "simple",
    // U11 (SPEC §7): the `voice_beta` preview flag. OFF (default) ⇒ no mic emitted (byte-identical);
    // ON ⇒ an inert, disabled mic with a "coming soon" tooltip renders before the send button.
    voiceBeta: Boolean = false,
) {
    // `/` slash palette (U4): floats above the composer whenever the input starts with "/", listing
    // the LIVE registry (faces / backends / one-click init) from GET /capabilities (MS8-G1), filtered
    // by whatever follows the slash. Selecting clears the composer, then applies the parent's action.
    if (input.startsWith("/")) {
        SlashPaletteOverlay(
            query = input.drop(1),
            capabilities = capabilities,
            onPickFace = { id -> onInputChange(""); onPickFace(id) },
            onPickBackend = { name -> onInputChange(""); onPickBackend(name) },
            onRunInit = { onInputChange(""); onRunInit() },
        )
        Spacer(Modifier.height(8.dp))
    }

    // input row
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(
            value = input,
            onValueChange = onInputChange,
            placeholder = {
                val placeholder = if (useSimplePlaceholder(experienceLevel)) {
                    stringResource(Res.string.chat_message_placeholder_simple)
                } else {
                    stringResource(Res.string.chat_message_placeholder)
                }
                Text(placeholder, color = LocalBoBClawColors.textMuted)
            },
            enabled = enabled,
            textStyle = BoBClawType.body,
            shape = BoBClawShapes.control,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { onSend() }),
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
            // Enter sends; Shift+Enter inserts a newline. (imeAction doesn't catch a hardware Enter
            // on desktop, so intercept the key event directly.)
            modifier = Modifier
                .weight(1f)
                .onPreviewKeyEvent { ev ->
                    if (ev.type == KeyEventType.KeyDown && ev.key == Key.Enter && !ev.isShiftPressed) {
                        onSend()
                        true
                    } else {
                        false
                    }
                },
        )
        // U11: inert voice-input affordance (disabled, "coming soon") — only when voice_beta is on.
        // Guarded so flag-off emits NOTHING here: the input row is byte-identical to today.
        if (voiceAffordancesVisible(voiceBeta)) {
            Spacer(Modifier.width(8.dp))
            VoiceMicButton(
                tooltip = stringResource(Res.string.placeholder_coming_soon),
                contentDescription = stringResource(Res.string.voice_mic_label),
            )
        }
        Spacer(Modifier.width(8.dp))
        if (generating) {
            OutlinedButton(
                onClick = onStop,
                shape = BoBClawShapes.control,
                colors = ButtonDefaults.outlinedButtonColors(contentColor = LocalBoBClawColors.alert),
            ) { Text(stringResource(Res.string.chat_stop), style = BoBClawType.label) }
        } else {
            Button(
                onClick = onSend,
                enabled = enabled && input.isNotBlank(),
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
