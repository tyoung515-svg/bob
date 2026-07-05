package com.bobclaw.ui.tiles

import com.bobclaw.shared.resources.*

import org.jetbrains.compose.resources.stringResource

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bobclaw.model.ApprovalItem
import com.bobclaw.network.RestClient
import com.bobclaw.ui.components.Tile
import com.bobclaw.ui.theme.BoBClawColors
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private val DenyRed = Color(0xFFE74C3C)

@Composable
fun ApprovalsTile(
    restClient: RestClient?,
    narrow: Boolean = false,
    modifier: Modifier = Modifier,
) {
    var items by remember { mutableStateOf<List<ApprovalItem>?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(restClient) {
        while (true) {
            if (restClient == null) {
                loading = false
                error = "Not configured"
                delay(10_000)
                continue
            }
            loading = true
            error = null
            try {
                items = restClient.getApprovals()
                loading = false
            } catch (e: Exception) {
                items = null
                error = e.message
                loading = false
            }
            delay(10_000)
        }
    }

    val pending = items?.filter { it.status == "pending" } ?: emptyList()

    if (narrow) {
        Tile(title = stringResource(Res.string.approvals_title), modifier = modifier) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = when {
                        loading && items == null -> stringResource(Res.string.approvals_loading_dots)
                        error != null && items == null -> "?"
                        else -> "${pending.size} pending"
                    },
                    color = if (pending.isNotEmpty()) BoBClawColors.AccentGreen else BoBClawColors.TextSecondary,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium,
                )
                if (pending.isNotEmpty()) {
                    Spacer(Modifier.width(6.dp))
                    Text(text = "●", color = BoBClawColors.AccentGreen, fontSize = 10.sp)
                }
            }
        }
    } else {
        Tile(title = stringResource(Res.string.approvals_title), modifier = modifier) {
            when {
                loading && items == null -> {
                    Text(stringResource(Res.string.approvals_loading), color = BoBClawColors.TextSecondary, fontSize = 13.sp)
                }
                error != null && items == null -> {
                    Text("Failed: $error", color = DenyRed, fontSize = 12.sp)
                }
                pending.isEmpty() -> {
                    Text(stringResource(Res.string.approvals_no_pending), color = BoBClawColors.TextSecondary, fontSize = 13.sp)
                }
                else -> {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 200.dp)
                            .verticalScroll(rememberScrollState())
                    ) {
                        pending.forEach { approval ->
                            ApprovalCard(approval, restClient)
                            Spacer(Modifier.height(6.dp))
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ApprovalCard(approval: ApprovalItem, restClient: RestClient?) {
    var decisionState by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(BoBClawColors.GlassFill, RoundedCornerShape(8.dp))
            .padding(10.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = approval.actionType,
                color = BoBClawColors.AccentGreen,
                fontSize = 11.sp,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = approval.details?.toString() ?: "",
                color = BoBClawColors.TextSecondary,
                fontSize = 10.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(
                text = stringResource(Res.string.approvals_approve),
                color = BoBClawColors.KpiGreen,
                fontSize = 11.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier
                    .clickable(enabled = decisionState == null) {
                        decisionState = "approved"
                        scope.launch {
                            try {
                                restClient?.postApprovalDecision(approval.id, "approve")
                            } catch (e: Exception) {
                                decisionState = null
                            }
                        }
                    }
                    .background(BoBClawColors.KpiGreen.copy(alpha = 0.15f), RoundedCornerShape(4.dp))
                    .padding(horizontal = 10.dp, vertical = 3.dp),
            )
            Text(
                text = stringResource(Res.string.approvals_deny),
                color = DenyRed,
                fontSize = 11.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier
                    .clickable(enabled = decisionState == null) {
                        decisionState = "denied"
                        scope.launch {
                            try {
                                restClient?.postApprovalDecision(approval.id, "reject")
                            } catch (e: Exception) {
                                decisionState = null
                            }
                        }
                    }
                    .background(DenyRed.copy(alpha = 0.15f), RoundedCornerShape(4.dp))
                    .padding(horizontal = 10.dp, vertical = 3.dp),
            )
            if (decisionState != null) {
                Spacer(Modifier.width(4.dp))
                Text(
                    text = if (decisionState == "approved") "✓" else "✗",
                    color = if (decisionState == "approved") BoBClawColors.KpiGreen else DenyRed,
                    fontSize = 12.sp,
                )
            }
        }
    }
}
