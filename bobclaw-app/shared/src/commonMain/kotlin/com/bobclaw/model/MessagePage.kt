package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class MessagePage(
    // Gateway envelope: {"items":[...],"has_more":bool}. Newest-first; no nextCursor.
    // Next (older) page cursor = id of the oldest item in messages, computed client-side, passed as `before`.
    @SerialName("items") val messages: List<Message>,
    @SerialName("has_more") val hasMore: Boolean = false
)
