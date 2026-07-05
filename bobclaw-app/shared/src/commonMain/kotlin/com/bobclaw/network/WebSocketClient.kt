package com.bobclaw.network

import com.bobclaw.model.ClientMessage
import com.bobclaw.model.ServerMessage
import io.ktor.client.HttpClient
import io.ktor.client.plugins.websocket.WebSockets
import io.ktor.client.plugins.websocket.webSocketSession
import io.ktor.client.request.header
import io.ktor.http.HttpHeaders
import io.ktor.http.Url
import io.ktor.http.takeFrom
import io.ktor.serialization.kotlinx.KotlinxWebsocketSerializationConverter
import io.ktor.websocket.Frame
import io.ktor.websocket.WebSocketSession
import io.ktor.websocket.close
import io.ktor.websocket.readText
import io.ktor.websocket.send
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json

class BoBClawWebSocket(private val url: String) {
    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
        classDiscriminator = "type"
    }

    private val httpClient = HttpClient {
        install(WebSockets) {
            contentConverter = KotlinxWebsocketSerializationConverter(json)
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val sessionMutex = Mutex()
    private var activeSession: WebSocketSession? = null
    private var connectJob: Job? = null
    private var heartbeatJob: Job? = null
    private var authToken: String? = null

    private val _incomingMessages = MutableSharedFlow<ServerMessage>(
        replay = 0,
        extraBufferCapacity = 64
    )
    val incomingMessages: SharedFlow<ServerMessage> = _incomingMessages.asSharedFlow()

    fun connect(token: String) {
        authToken = token
        println("[ws] connect() (alreadyActive=${connectJob?.isActive == true})")
        if (connectJob?.isActive == true) return

        connectJob = scope.launch {
            reconnectLoop()
        }
    }

    suspend fun sendMessage(conversationId: String, content: String, faceId: String?, locale: String? = null) {
        sendClientMessage(
            ClientMessage.ChatMessage(
                conversationId = conversationId,
                content = content,
                faceId = faceId,
                locale = locale
            )
        )
    }

    suspend fun switchLocale(conversationId: String, locale: String) {
        sendClientMessage(
            ClientMessage.SwitchLocale(
                conversationId = conversationId,
                locale = locale
            )
        )
    }

    suspend fun switchFace(conversationId: String, faceId: String, faceName: String? = null) {
        sendClientMessage(
            ClientMessage.SwitchFace(
                conversationId = conversationId,
                faceId = faceId,
                faceName = faceName
            )
        )
    }

    suspend fun switchModel(conversationId: String, model: String, backend: String) {
        sendClientMessage(
            ClientMessage.SwitchModel(
                conversationId = conversationId,
                model = model,
                backend = backend
            )
        )
    }

    suspend fun switchProfile(conversationId: String, profile: String) {
        sendClientMessage(
            ClientMessage.SwitchProfile(
                conversationId = conversationId,
                profile = profile
            )
        )
    }

    suspend fun sendApproval(approvalId: String, decision: String) {
        sendClientMessage(
            ClientMessage.ApprovalResponse(
                approvalId = approvalId,
                decision = decision
            )
        )
    }

    suspend fun stopGeneration() {
        sendClientMessage(ClientMessage.StopGeneration())
    }

    fun disconnect() {
        connectJob?.cancel()
        heartbeatJob?.cancel()
        scope.launch {
            sessionMutex.withLock {
                activeSession?.close()
                activeSession = null
            }
        }
    }

    private suspend fun reconnectLoop() {
        // Parse once. NOTE: takeFrom() does NOT carry a non-default port for ws:// — it leaves
        // the builder at ws:80 and the connect is refused — so set the port explicitly below.
        val target = Url(url)
        var backoffMs = 1_000L
        while (scope.isActive) {
            val token = authToken
            if (token.isNullOrBlank()) {
                delay(1_000)
                continue
            }

            try {
                val session = httpClient.webSocketSession {
                    url {
                        takeFrom(target)
                        port = target.port
                    }
                    header(HttpHeaders.Authorization, "Bearer $token")
                }

                sessionMutex.withLock {
                    activeSession = session
                }

                backoffMs = 1_000L
                startHeartbeat(session)
                println("[ws] connected -> ${target.host}:${target.port}${target.encodedPath}")

                for (frame in session.incoming) {
                    val text = (frame as? Frame.Text)?.readText() ?: continue
                    println("[ws] <- ${text.take(220)}")
                    val parsed = runCatching {
                        json.decodeFromString(ServerMessage.serializer(), text)
                    }.getOrElse {
                        println("[ws] DECODE FAIL: ${it.message}")
                        ServerMessage.Error(
                            code = "decode_error",
                            message = it.message ?: "Failed to parse server message"
                        )
                    }
                    _incomingMessages.emit(parsed)
                }
            } catch (t: Throwable) {
                println("[ws] socket error: ${t.message}")
                _incomingMessages.emit(
                    ServerMessage.Error(
                        code = "socket_error",
                        message = t.message ?: "WebSocket disconnected"
                    )
                )
            } finally {
                heartbeatJob?.cancel()
                sessionMutex.withLock {
                    activeSession?.close()
                    activeSession = null
                }
            }

            println("[ws] reconnecting in ${backoffMs}ms")
            delay(backoffMs)
            backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
        }
    }

    private fun startHeartbeat(session: WebSocketSession) {
        heartbeatJob?.cancel()
        heartbeatJob = scope.launch {
            while (isActive) {
                delay(30_000)
                runCatching {
                    session.send(Frame.Ping(byteArrayOf()))
                }
            }
        }
    }

    private suspend fun sendClientMessage(message: ClientMessage) {
        val payload = json.encodeToString(ClientMessage.serializer(), message)
        val session = sessionMutex.withLock { activeSession }
        if (session == null) {
            println("[ws] SEND DROPPED (not connected): ${payload.take(140)}")
            error("WebSocket is not connected. Call connect(token) first.")
        }
        println("[ws] -> ${payload.take(220)}")
        session.send(Frame.Text(payload))
    }
}
