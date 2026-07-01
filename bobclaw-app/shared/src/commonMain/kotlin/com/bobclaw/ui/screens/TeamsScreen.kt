package com.bobclaw.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextFieldColors
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
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import com.bobclaw.model.BackendPalette
import com.bobclaw.model.ProtocolBounds
import com.bobclaw.model.Team
import com.bobclaw.model.TeamDraft
import com.bobclaw.model.TeamSlot
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.BoBClawShapes
import com.bobclaw.ui.theme.BoBClawType
import com.bobclaw.ui.theme.GradientBackground
import com.bobclaw.ui.theme.LocalBoBClawColors
import kotlinx.coroutines.launch

private val ErrorRed = Color(0xFFE74C3C)
private val OkGreen = Color(0xFF2ECC71)
private val SHAPES = listOf("fusion", "sequential", "debate")

// ── immutable draft edits (single source the form + refine both update) ──────────
private fun TeamDraft.withName(n: String) = copy(name = n)

private fun TeamDraft.addSlot(role: String, backend: String): TeamDraft =
    copy(roles = roles + (role to ((roles[role] ?: emptyList()) + TeamSlot(backend = backend))))

private fun TeamDraft.setSlotBackend(role: String, idx: Int, backend: String): TeamDraft =
    editSlot(role, idx) { it.copy(backend = backend) }

private fun TeamDraft.setSlotRolePrompt(role: String, idx: Int, rolePrompt: String): TeamDraft =
    editSlot(role, idx) { it.copy(rolePrompt = rolePrompt) }

private fun TeamDraft.editSlot(role: String, idx: Int, f: (TeamSlot) -> TeamSlot): TeamDraft {
    val list = (roles[role] ?: return this).toMutableList()
    if (idx !in list.indices) return this
    list[idx] = f(list[idx])
    return copy(roles = roles + (role to list))
}

private fun TeamDraft.removeSlot(role: String, idx: Int): TeamDraft {
    val list = (roles[role] ?: return this).toMutableList()
    if (idx !in list.indices) return this
    list.removeAt(idx)
    return if (list.isEmpty()) copy(roles = roles - role) else copy(roles = roles + (role to list))
}

private fun TeamDraft.setShape(shape: String?): TeamDraft = copy(shape = shape)
private fun TeamDraft.setMaxUsd(v: Double?): TeamDraft =
    copy(protocolBounds = (protocolBounds ?: ProtocolBounds()).copy(maxUsd = v))
private fun TeamDraft.setGrounding(g: String?): TeamDraft =
    copy(protocolBounds = (protocolBounds ?: ProtocolBounds()).copy(grounding = g))

@Composable
private fun fieldColors(): TextFieldColors = OutlinedTextFieldDefaults.colors(
    focusedContainerColor = LocalBoBClawColors.surfaceCard,
    unfocusedContainerColor = LocalBoBClawColors.surfaceCard,
    disabledContainerColor = LocalBoBClawColors.surfaceCard,
    focusedTextColor = LocalBoBClawColors.textBody,
    unfocusedTextColor = LocalBoBClawColors.textBody,
    focusedBorderColor = LocalBoBClawColors.accent,
    unfocusedBorderColor = LocalBoBClawColors.borderControl,
    cursorColor = LocalBoBClawColors.accent,
)

/**
 * A text field that OWNS its editing buffer (TextFieldValue), syncing from [value] only
 * when it changes externally (e.g. the assistant refine writes the draft). Plain
 * value:String fields backed by the heavy immutable draft drop pasted text — this fixes
 * it and supports large multi-line role prompts.
 */
@Composable
private fun DraftTextField(
    value: String,
    onChange: (String) -> Unit,
    placeholder: String,
    modifier: Modifier = Modifier,
    singleLine: Boolean = false,
    minLines: Int = 1,
    maxLines: Int = Int.MAX_VALUE,
) {
    val colors = LocalBoBClawColors
    var tfv by remember { mutableStateOf(TextFieldValue(value)) }
    LaunchedEffect(value) {
        if (value != tfv.text) tfv = TextFieldValue(value, selection = TextRange(value.length))
    }
    OutlinedTextField(
        value = tfv,
        onValueChange = { tfv = it; onChange(it.text) },
        singleLine = singleLine,
        minLines = minLines,
        maxLines = maxLines,
        placeholder = { Text(placeholder, color = colors.textMuted) },
        textStyle = BoBClawType.body,
        shape = BoBClawShapes.control,
        colors = fieldColors(),
        modifier = modifier,
    )
}

/**
 * Profile builder (DESIGN §6.4 rail "Teams"). One working [TeamDraft] both the
 * assistant chat and the form edit: each role is a roster of backend slots, each slot
 * carries an optional **role prompt** (how that spot acts), and a **shape** + bounds
 * turn the roster into a coordinated council. Save persists a custom profile; built-ins
 * are read-only.
 */
@Composable
fun TeamsScreen(
    restClient: RestClient?,
    modifier: Modifier = Modifier,
) {
    var palette by remember { mutableStateOf<BackendPalette?>(null) }
    var profiles by remember { mutableStateOf<List<Team>?>(null) }
    var loadError by remember { mutableStateOf<String?>(null) }
    var reloadKey by remember { mutableStateOf(0) }

    var draft by remember { mutableStateOf(TeamDraft()) }
    var saving by remember { mutableStateOf(false) }
    var saveError by remember { mutableStateOf<String?>(null) }
    var saveOk by remember { mutableStateOf<String?>(null) }

    var message by remember { mutableStateOf("") }
    var refining by remember { mutableStateOf(false) }
    val chat = remember { mutableStateListOf<Pair<Boolean, String>>() }

    val scope = rememberCoroutineScope()
    val colors = LocalBoBClawColors

    LaunchedEffect(restClient, reloadKey) {
        if (restClient == null) {
            loadError = "Not configured — no gateway URL set"
            return@LaunchedEffect
        }
        loadError = null
        try {
            palette = restClient.getBackends()
            profiles = restClient.getProfiles()
        } catch (e: Exception) {
            loadError = e.message ?: "Unknown error"
        }
    }

    GradientBackground(modifier = modifier) {
        Column(Modifier.fillMaxSize().padding(20.dp).verticalScroll(rememberScrollState())) {
            Text("Teams & profiles", style = BoBClawType.title, color = colors.textPrimary)
            Spacer(Modifier.height(4.dp))
            Text(
                "Build a fleet — bind roles to backends, instruct each spot with a role prompt, " +
                    "and pick a shape to make them deliberate.",
                style = BoBClawType.monoCaption, color = colors.textSecondary,
            )

            if (loadError != null) {
                Spacer(Modifier.height(12.dp))
                Text("Failed to load: $loadError", style = BoBClawType.body, color = ErrorRed)
            }

            val pal = palette
            if (pal != null) {
                val backendOptions = pal.items.map { it.backend }

                // ── Assistant chat ──────────────────────────────────────────
                Spacer(Modifier.height(20.dp))
                Text("Assistant", style = BoBClawType.body, color = colors.textPrimary,
                    fontWeight = FontWeight.Bold)
                Text("Describe or adjust the fleet (incl. role prompts + shape); it edits the draft below.",
                    style = BoBClawType.monoCaption, color = colors.textMuted)
                Spacer(Modifier.height(8.dp))

                chat.forEach { (fromUser, text) -> ChatBubble(fromUser, text) }
                if (refining) ChatBubble(false, "…thinking")

                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = message,
                    onValueChange = { message = it },
                    singleLine = true,
                    placeholder = { Text("e.g. a fusion council: optimist + skeptic + synthesizer",
                        color = colors.textMuted) },
                    textStyle = BoBClawType.body,
                    shape = BoBClawShapes.control,
                    colors = fieldColors(),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(6.dp))
                Button(
                    enabled = !refining,
                    onClick = {
                        val msg = message.trim()
                        if (msg.isNotBlank()) {
                            val history = chat.map { com.bobclaw.model.ChatTurn(if (it.first) "user" else "assistant", it.second) }
                            chat.add(true to msg)
                            message = ""; refining = true; saveError = null; saveOk = null
                            scope.launch {
                                try {
                                    val seed = draft.takeIf { it.roles.isNotEmpty() || it.name.isNotBlank() }
                                    val res = restClient!!.refineTeam(msg, history, seed)
                                    draft = res.draft
                                    chat.add(false to (res.error?.let { "⚠ $it" }
                                        ?: res.reply.ifBlank { "Updated the draft." }))
                                } catch (e: Exception) {
                                    chat.add(false to "⚠ ${e.message ?: "refine failed"}")
                                } finally {
                                    refining = false
                                }
                            }
                        }
                    },
                ) { Text(if (refining) "Thinking…" else "Send") }

                // ── Draft form ──────────────────────────────────────────────
                Spacer(Modifier.height(24.dp))
                Text("Draft", style = BoBClawType.body, color = colors.textPrimary,
                    fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(8.dp))
                DraftTextField(
                    value = draft.name,
                    onChange = { draft = draft.withName(it); saveError = null; saveOk = null },
                    placeholder = "name (lowercase-slug)",
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )

                pal.roles.forEach { role ->
                    Spacer(Modifier.height(10.dp))
                    Text(role, style = BoBClawType.monoCaption, color = colors.textMuted)
                    (draft.roles[role] ?: emptyList()).forEachIndexed { idx, slot ->
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Dropdown(
                                options = backendOptions,
                                selected = slot.backend,
                                placeholder = "— pick backend —",
                                allowNone = false,
                                onSelect = { draft = draft.setSlotBackend(role, idx, it) },
                            )
                            if (slot.escalationChain.isNotEmpty()) {
                                Spacer(Modifier.width(8.dp))
                                Text("↳ " + slot.escalationChain.joinToString(" → "),
                                    style = BoBClawType.monoCaption, color = colors.textMuted)
                            }
                            Spacer(Modifier.weight(1f))
                            Text(
                                "✕",
                                style = BoBClawType.monoCaption,
                                color = ErrorRed,
                                modifier = Modifier.clip(BoBClawShapes.cell)
                                    .clickable { draft = draft.removeSlot(role, idx) }
                                    .padding(horizontal = 8.dp, vertical = 4.dp),
                            )
                        }
                        DraftTextField(
                            value = slot.rolePrompt,
                            onChange = { draft = draft.setSlotRolePrompt(role, idx, it) },
                            placeholder = "role prompt — how this spot should act (optional)",
                            minLines = 2,
                            maxLines = 10,
                            modifier = Modifier.fillMaxWidth().padding(start = 8.dp, bottom = 4.dp),
                        )
                    }
                    Text(
                        "+ add $role",
                        style = BoBClawType.monoCaption,
                        color = colors.accent,
                        modifier = Modifier.clip(BoBClawShapes.cell)
                            .clickable { draft = draft.addSlot(role, backendOptions.firstOrNull() ?: "local") }
                            .padding(horizontal = 8.dp, vertical = 4.dp),
                    )
                }

                // ── Coordination (shape + bounds) ───────────────────────────
                Spacer(Modifier.height(14.dp))
                Text("Coordination", style = BoBClawType.monoCaption, color = colors.textMuted)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("shape", style = BoBClawType.body, color = colors.textSecondary)
                    Spacer(Modifier.width(10.dp))
                    Dropdown(
                        options = SHAPES,
                        selected = draft.shape ?: "",
                        placeholder = "— roster (no council) —",
                        allowNone = true,
                        onSelect = { draft = draft.setShape(it.ifBlank { null }) },
                    )
                }
                if (draft.shape != null) {
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(
                            value = draft.protocolBounds?.maxUsd?.toString() ?: "",
                            onValueChange = { draft = draft.setMaxUsd(it.toDoubleOrNull()) },
                            singleLine = true,
                            placeholder = { Text("max $/run", color = colors.textMuted) },
                            textStyle = BoBClawType.body,
                            shape = BoBClawShapes.control,
                            colors = fieldColors(),
                            modifier = Modifier.width(150.dp),
                        )
                        Spacer(Modifier.width(10.dp))
                        Text("grounding", style = BoBClawType.body, color = colors.textSecondary)
                        Spacer(Modifier.width(8.dp))
                        Dropdown(
                            options = listOf("on", "off"),
                            selected = draft.protocolBounds?.grounding ?: "",
                            placeholder = "default",
                            allowNone = true,
                            onSelect = { draft = draft.setGrounding(it.ifBlank { null }) },
                        )
                    }
                    Text("loop bounds apply once P3b lands; role prompts + shape are live now.",
                        style = BoBClawType.monoCaption, color = colors.textMuted,
                        modifier = Modifier.padding(top = 2.dp))
                }

                Spacer(Modifier.height(14.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Button(
                        enabled = !saving,
                        onClick = {
                            val cleaned = draft.roles
                                .mapValues { (_, slots) -> slots.filter { it.backend.isNotBlank() } }
                                .filterValues { it.isNotEmpty() }
                            when {
                                draft.name.isBlank() -> saveError = "name is required"
                                cleaned.isEmpty() -> saveError = "add at least one role + backend"
                                else -> {
                                    saving = true; saveError = null; saveOk = null
                                    scope.launch {
                                        try {
                                            val created = restClient!!.createProfile(
                                                draft.copy(name = draft.name.trim(), roles = cleaned)
                                            )
                                            saveOk = "Saved '${created.name}'"
                                            draft = TeamDraft(); chat.clear(); reloadKey++
                                        } catch (e: Exception) {
                                            saveError = e.message ?: "Save failed"
                                        } finally {
                                            saving = false
                                        }
                                    }
                                }
                            }
                        },
                    ) { Text(if (saving) "Saving…" else "Save") }
                    Spacer(Modifier.width(12.dp))
                    val err = saveError
                    val ok = saveOk
                    if (err != null) Text(err, style = BoBClawType.monoCaption, color = ErrorRed)
                    else if (ok != null) Text(ok, style = BoBClawType.monoCaption, color = OkGreen)
                }

                // ── All profiles ────────────────────────────────────────────
                Spacer(Modifier.height(24.dp))
                Text("All profiles", style = BoBClawType.body, color = colors.textPrimary,
                    fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(8.dp))
                val list = profiles
                if (list == null) {
                    Text("Loading…", style = BoBClawType.body, color = colors.textSecondary)
                } else {
                    list.forEach { team ->
                        TeamRow(team, onDelete = {
                            scope.launch {
                                try {
                                    restClient!!.deleteProfile(team.name)
                                    reloadKey++
                                } catch (_: Exception) {
                                }
                            }
                        })
                    }
                }
            }
        }
    }
}

@Composable
private fun ChatBubble(fromUser: Boolean, text: String) {
    val colors = LocalBoBClawColors
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp),
        horizontalArrangement = if (fromUser) Arrangement.End else Arrangement.Start,
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 480.dp)
                .clip(BoBClawShapes.card)
                .background(if (fromUser) colors.accent else colors.surfaceCard, BoBClawShapes.card)
                .padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            Text(text, style = BoBClawType.body,
                color = if (fromUser) colors.onAccent else colors.textBody)
        }
    }
}

@Composable
private fun Dropdown(
    options: List<String>,
    selected: String,
    placeholder: String,
    allowNone: Boolean,
    onSelect: (String) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    val colors = LocalBoBClawColors
    Box {
        Text(
            text = selected.ifBlank { placeholder },
            style = BoBClawType.body,
            color = if (selected.isBlank()) colors.textMuted else colors.accent,
            modifier = Modifier
                .clip(BoBClawShapes.cell)
                .background(colors.surfaceCard, BoBClawShapes.cell)
                .clickable { expanded = true }
                .padding(horizontal = 12.dp, vertical = 8.dp),
        )
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            if (allowNone) {
                DropdownMenuItem(text = { Text(placeholder) }, onClick = { onSelect(""); expanded = false })
            }
            options.forEach { opt ->
                DropdownMenuItem(text = { Text(opt) }, onClick = { onSelect(opt); expanded = false })
            }
        }
    }
}

@Composable
private fun TeamRow(team: Team, onDelete: () -> Unit) {
    val colors = LocalBoBClawColors
    Column(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(team.name, style = BoBClawType.body, color = colors.textPrimary,
                fontWeight = FontWeight.Medium)
            Spacer(Modifier.width(8.dp))
            Text(
                if (team.builtin) "built-in" else "custom",
                style = BoBClawType.monoCaption,
                color = if (team.builtin) colors.textMuted else colors.accent,
            )
            if (team.shape != null) {
                Spacer(Modifier.width(8.dp))
                Text(team.shape, style = BoBClawType.monoCaption, color = colors.success)
            }
            Spacer(Modifier.weight(1f))
            if (!team.builtin) {
                Text(
                    "Delete",
                    style = BoBClawType.monoCaption,
                    color = ErrorRed,
                    modifier = Modifier
                        .clip(BoBClawShapes.cell)
                        .clickable(onClick = onDelete)
                        .padding(horizontal = 8.dp, vertical = 4.dp),
                )
            }
        }
        team.roles.forEach { (role, slots) ->
            val txt = slots.joinToString(", ") { s ->
                s.backend + if (s.rolePrompt.isBlank()) "" else " (\"${s.rolePrompt.take(28)}\")"
            }
            Text("$role: $txt", style = BoBClawType.monoCaption, color = colors.textSecondary,
                modifier = Modifier.padding(start = 8.dp))
        }
        Spacer(Modifier.height(6.dp))
        Spacer(Modifier.fillMaxWidth().height(1.dp).background(colors.borderSection))
    }
}
