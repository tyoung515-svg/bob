package com.bobclaw.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.auth.AuthManager
import com.bobclaw.ui.components.Tile
import com.bobclaw.ui.theme.BoBClawColors
import com.bobclaw.ui.theme.GradientBackground
import kotlinx.coroutines.launch

/**
 * Password login with an optional TOTP code (leave TOTP blank when the gateway has it
 * disabled; fill it in when TOTP is enabled). On success calls [onLoggedIn]. Disables the
 * button while the login request is in flight; surfaces errors as red text.
 */
@Composable
fun LoginScreen(
    authManager: AuthManager,
    onLoggedIn: () -> Unit,
) {
    var password by remember { mutableStateOf("") }
    var totp by remember { mutableStateOf("") }
    var inFlight by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    fun submit() {
        if (inFlight || password.isBlank()) return
        inFlight = true
        error = null
        scope.launch {
            runCatching { authManager.login(password, totp.ifBlank { null }) }
                .onSuccess { onLoggedIn() }
                .onFailure { error = it.message ?: "Login failed" }
            inFlight = false
        }
    }

    GradientBackground {
        Box(modifier = Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
            Tile(
                title = "BoBClaw",
                modifier = Modifier.widthIn(max = 420.dp),
            ) {
                Text(
                    text = "Sign in",
                    color = BoBClawColors.TextPrimary,
                    fontSize = 20.sp,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.height(16.dp))
                OutlinedTextField(
                    value = password,
                    onValueChange = { password = it },
                    label = { Text("Password") },
                    singleLine = true,
                    enabled = !inFlight,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = { submit() }),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BoBClawColors.TextPrimary,
                        unfocusedTextColor = BoBClawColors.TextPrimary,
                        focusedBorderColor = BoBClawColors.AccentGreen,
                        unfocusedBorderColor = BoBClawColors.BorderSubtle,
                        focusedLabelColor = BoBClawColors.AccentGreen,
                        unfocusedLabelColor = BoBClawColors.TextSecondary,
                        cursorColor = BoBClawColors.AccentGreen,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(
                    value = totp,
                    onValueChange = { totp = it.filter(Char::isDigit).take(6) },
                    label = { Text("TOTP code (optional)") },
                    singleLine = true,
                    enabled = !inFlight,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = { submit() }),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BoBClawColors.TextPrimary,
                        unfocusedTextColor = BoBClawColors.TextPrimary,
                        focusedBorderColor = BoBClawColors.AccentGreen,
                        unfocusedBorderColor = BoBClawColors.BorderSubtle,
                        focusedLabelColor = BoBClawColors.AccentGreen,
                        unfocusedLabelColor = BoBClawColors.TextSecondary,
                        cursorColor = BoBClawColors.AccentGreen,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(16.dp))
                Button(
                    onClick = { submit() },
                    enabled = !inFlight && password.isNotBlank(),
                    colors = ButtonDefaults.buttonColors(containerColor = BoBClawColors.AccentGreen),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (inFlight) {
                        CircularProgressIndicator(
                            modifier = Modifier.height(18.dp),
                            color = BoBClawColors.GradientBottom,
                            strokeWidth = 2.dp,
                        )
                    } else {
                        Text("Log in")
                    }
                }
                if (error != null) {
                    Spacer(Modifier.height(12.dp))
                    Text(
                        text = error ?: "",
                        color = androidx.compose.ui.graphics.Color(0xFFE74C3C),
                        fontSize = 13.sp,
                    )
                }
            }
        }
    }
}
