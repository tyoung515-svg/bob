package com.bobclaw.ui.screens

import com.bobclaw.shared.resources.Res
import com.bobclaw.shared.resources.chat_canvas
import com.bobclaw.shared.resources.chat_clear
import com.bobclaw.shared.resources.chat_enter_url
import com.bobclaw.shared.resources.chat_go
import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.bobclaw.ui.components.IconGlyph
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.glassMorphism

/**
 * The collapsible right-hand artifact/canvas pane (MS8-A2 decomposition of `ChatScreen`): a title
 * bar with a close button, a mini-browser URL bar (Enter or "Go" navigates; "Clear" blanks it), and
 * the platform artifact surface. A [RowScope] extension so it keeps its `weight(1f)` share of the
 * chat Row exactly as it did inline. All canvas state is hoisted to the caller.
 */
@Composable
internal fun RowScope.ArtifactPanel(
    canvasHtml: String?,
    canvasUrl: String?,
    canvasUrlInput: String,
    onUrlInputChange: (String) -> Unit,
    onGo: () -> Unit,
    onClear: () -> Unit,
    onClose: () -> Unit,
    artifactRenderer: @Composable (html: String?, url: String?, modifier: Modifier) -> Unit,
) {
    Column(modifier = Modifier.weight(1f).fillMaxHeight()) {
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                stringResource(Res.string.chat_canvas),
                color = BoBClawColors.TextPrimary,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.weight(1f),
            )
            OutlinedButton(
                onClick = onClose,
                colors = ButtonDefaults.outlinedButtonColors(contentColor = BoBClawColors.TextSecondary),
            ) { IconGlyph(name = "x", tint = BoBClawColors.TextSecondary, size = 14.dp) }
        }
        Spacer(Modifier.height(8.dp))
        // URL bar — turns the canvas into a mini-browser. Enter or "Go" navigates; "Clear" blanks it.
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = canvasUrlInput,
                onValueChange = onUrlInputChange,
                singleLine = true,
                placeholder = { Text(stringResource(Res.string.chat_enter_url)) },
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Go),
                keyboardActions = KeyboardActions(onGo = { onGo() }),
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
                            onGo()
                            true
                        } else {
                            false
                        }
                    },
            )
            Spacer(Modifier.width(8.dp))
            Button(
                onClick = onGo,
                enabled = canvasUrlInput.isNotBlank(),
                colors = ButtonDefaults.buttonColors(containerColor = BoBClawColors.AccentGreen),
            ) { Text(stringResource(Res.string.chat_go)) }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(
                onClick = onClear,
                colors = ButtonDefaults.outlinedButtonColors(contentColor = BoBClawColors.TextSecondary),
            ) { Text(stringResource(Res.string.chat_clear)) }
        }
        Spacer(Modifier.height(8.dp))
        Box(modifier = Modifier.fillMaxSize().glassMorphism()) {
            artifactRenderer(canvasHtml, canvasUrl, Modifier.fillMaxSize())
        }
    }
}
