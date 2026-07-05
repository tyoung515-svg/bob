package com.bobclaw.ui.tiles

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.Conversation
import com.bobclaw.network.RestClient
import com.bobclaw.ui.theme.BoBClawColors
import kotlinx.coroutines.delay

@Composable
fun ConversationListTile(
    restClient: RestClient?,
    modifier: Modifier = Modifier,
    onOpenConversation: (String) -> Unit = {},
) {
    var conversations by remember { mutableStateOf<List<Conversation>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(restClient) {
        while (true) {
            if (restClient == null) {
                loading = false
                error = "Not configured — no gateway URL set"
                delay(10_000)
                continue
            }
            loading = true
            error = null
            try {
                conversations = restClient.getConversations(limit = 20, offset = 0)
                loading = false
            } catch (e: Exception) {
                conversations = null
                error = e.message ?: "Unknown error"
                loading = false
            }
            delay(10_000)
        }
    }

    SectionTile(title = stringResource(Res.string.conv_list_conversations), modifier = modifier) {
        if (loading && conversations == null) {
            Text(
                stringResource(Res.string.conv_list_loading_conversations),
                color = BoBClawColors.TextSecondary,
                fontSize = 13.sp,
            )
        } else if (error != null && conversations == null) {
            Text(
                "Failed: $error",
                color = BoBClawColors.TextSecondary,
                fontSize = 12.sp,
            )
        } else {
            val items = conversations
            if (items.isNullOrEmpty()) {
                Text(
                    stringResource(Res.string.conv_list_no_conversations_yet),
                    color = BoBClawColors.TextSecondary,
                    fontSize = 13.sp,
                )
            } else {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(max = 160.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    items.take(8).forEach { conv ->
                        ConversationRow(conv, onClick = { onOpenConversation(conv.id) })
                        Spacer(Modifier.height(6.dp))
                    }
                }
            }
        }
    }
}

@Composable
private fun ConversationRow(conv: Conversation, onClick: () -> Unit) {
    Column(modifier = Modifier.fillMaxWidth().clickable { onClick() }) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = conv.title ?: stringResource(Res.string.conv_list_untitled),
                color = BoBClawColors.TextPrimary,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }
        if (conv.lastMessagePreview != null) {
            Text(
                text = conv.lastMessagePreview,
                color = BoBClawColors.TextSecondary,
                fontSize = 10.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
