package com.bobclaw.ui.tiles

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.Idea
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.BoBClawColors
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private data class LocalIdea(
    val body: String,
    val tags: List<String>,
)

private val availableTags = listOf("feature", "bug", "refactor", "docs", "optimization")
private val TagBg = Color(0x33FFFFFF)
private val TagActiveBg = Color(0x44FFFFFF)
// InputBg / InputBorder were top-level `val`s reading BoBClawColors.GlassFill/.BorderSubtle. Those
// aliases are now composable-only accessors (lane 4b), so a module-level init can't read them —
// they are now read into locals inside the @Composable IdeaInboxTile (see below).

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun IdeaInboxTile(
    restClient: RestClient? = null,
    modifier: Modifier = Modifier,
) {
    val localIdeas = remember { mutableStateListOf<LocalIdea>() }
    var backendIdeas by remember { mutableStateOf<List<Idea>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var inputText by remember { mutableStateOf("") }
    val selectedTags = remember { mutableStateListOf<String>() }
    val scope = rememberCoroutineScope()

    // Input-field surface/border, read here in composable scope (the alias getters are now
    // composable-only accessors). Were top-level `val`s before lane 4b.
    val inputBg = BoBClawColors.GlassFill
    val inputBorder = BoBClawColors.BorderSubtle

    val isLocalOnly = restClient == null

    LaunchedEffect(restClient) {
        if (restClient == null) return@LaunchedEffect
        while (true) {
            loading = true
            error = null
            try {
                backendIdeas = restClient.getIdeas()
                loading = false
            } catch (e: Exception) {
                backendIdeas = null
                error = e.message
                loading = false
            }
            delay(10_000)
        }
    }

    fun addIdea(text: String, tags: List<String>) {
        if (isLocalOnly) {
            localIdeas.add(0, LocalIdea(body = text, tags = tags))
        } else {
            scope.launch {
                try {
                    restClient?.createIdea(body = text, tags = tags)
                    backendIdeas = restClient?.getIdeas()
                } catch (_: Exception) {
                    localIdeas.add(0, LocalIdea(body = text, tags = tags))
                }
            }
        }
    }

    SectionTile(title = stringResource(Res.string.idea_inbox_title), modifier = modifier) {
        Column(modifier = Modifier.fillMaxWidth()) {
            if (isLocalOnly) {
                Text(
                    stringResource(Res.string.idea_inbox_local_only),
                    color = BoBClawColors.TextSecondary,
                    fontSize = 9.sp,
                    modifier = Modifier.padding(bottom = 4.dp),
                )
            }

            val hasBackendItems = !backendIdeas.isNullOrEmpty()
            val hasLocalItems = localIdeas.isNotEmpty()
            val showEmpty = !loading && !hasBackendItems && !hasLocalItems && error == null
            val showError = error != null && !hasBackendItems && !hasLocalItems && !isLocalOnly

            when {
                loading && !hasBackendItems && !hasLocalItems -> {
                    Text(stringResource(Res.string.idea_inbox_loading), color = BoBClawColors.TextSecondary, fontSize = 12.sp)
                }
                showError -> {
                    Text("Failed: $error", color = BoBClawColors.TextSecondary, fontSize = 12.sp)
                }
                showEmpty -> {
                    Text(
                        stringResource(Res.string.idea_inbox_no_ideas),
                        color = BoBClawColors.TextSecondary,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(bottom = 8.dp),
                    )
                }
                else -> {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 140.dp)
                            .verticalScroll(rememberScrollState())
                    ) {
                        localIdeas.forEach { idea ->
                            IdeaCard(idea.body, idea.tags)
                            Spacer(Modifier.height(6.dp))
                        }
                        backendIdeas?.forEach { idea ->
                            IdeaCard(idea.body, idea.tags)
                            Spacer(Modifier.height(6.dp))
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                }
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = inputText,
                    onValueChange = { inputText = it },
                    placeholder = {
                        Text(stringResource(Res.string.idea_inbox_placeholder), color = BoBClawColors.TextSecondary, fontSize = 12.sp)
                    },
                    modifier = Modifier.weight(1f),
                    singleLine = true,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BoBClawColors.TextPrimary,
                        unfocusedTextColor = BoBClawColors.TextPrimary,
                        cursorColor = BoBClawColors.AccentGreen,
                        focusedBorderColor = BoBClawColors.AccentGreen,
                        unfocusedBorderColor = inputBorder,
                        focusedContainerColor = inputBg,
                        unfocusedContainerColor = inputBg,
                    ),
                    shape = RoundedCornerShape(8.dp),
                    textStyle = TextStyle(fontSize = 12.sp),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = {
                        if (inputText.isNotBlank()) {
                            addIdea(inputText.trim(), selectedTags.toList())
                            inputText = ""
                            selectedTags.clear()
                        }
                    }),
                )
                Spacer(Modifier.width(6.dp))
                Text(
                    text = "+",
                    color = if (inputText.isNotBlank()) BoBClawColors.AccentGreen else BoBClawColors.TextSecondary,
                    fontSize = 20.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier
                        .clickable(enabled = inputText.isNotBlank()) {
                            addIdea(inputText.trim(), selectedTags.toList())
                            inputText = ""
                            selectedTags.clear()
                        }
                        .padding(8.dp),
                )
            }

            Spacer(Modifier.height(6.dp))

            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                availableTags.forEach { tag ->
                    val isSelected = tag in selectedTags
                    Text(
                        text = tag,
                        color = if (isSelected) BoBClawColors.AccentGreen else BoBClawColors.TextSecondary,
                        fontSize = 10.sp,
                        fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Normal,
                        modifier = Modifier
                            .clickable {
                                if (isSelected) selectedTags.remove(tag) else selectedTags.add(tag)
                            }
                            .background(
                                if (isSelected) TagActiveBg else TagBg,
                                RoundedCornerShape(4.dp),
                            )
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                    )
                }
            }
        }
    }
}

@Composable
private fun IdeaCard(body: String, tags: List<String>) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TagBg, RoundedCornerShape(6.dp))
            .padding(8.dp),
    ) {
        Text(
            text = body,
            color = BoBClawColors.TextPrimary,
            fontSize = 12.sp,
        )
        if (tags.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                tags.forEach { tag ->
                    Text(
                        text = tag,
                        color = BoBClawColors.AccentGreen,
                        fontSize = 9.sp,
                        modifier = Modifier
                            .background(TagActiveBg, RoundedCornerShape(3.dp))
                            .padding(horizontal = 4.dp, vertical = 1.dp),
                    )
                }
            }
        }
    }
}
