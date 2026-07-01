package com.bobclaw

import com.bobclaw.auth.AuthManager
import com.bobclaw.model.ServerMessage
import com.bobclaw.network.BoBClawWebSocket
import com.bobclaw.network.Config
import com.bobclaw.network.RestClient
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Headless end-to-end smoke for the KMM networking layer against the LIVE gateway.
 * Exercises the exact client code the desktop UI uses (RestClient / AuthManager /
 * BoBClawWebSocket + kotlinx serialization) with NO Compose/GUI, so wire bugs surface
 * as plain stdout instead of a GUI banner. Run via `:desktopApp:smoke`.
 *
 * Creds from env BC_PASSWORD + BC_TOTP — run with --no-daemon so env propagates to the fork.
 */
fun main() = runBlocking {
    val password = System.getenv("BC_PASSWORD").orEmpty()
    val totp = System.getenv("BC_TOTP").orEmpty()
    require(password.isNotBlank()) { "BC_PASSWORD env missing" }

    val rest = RestClient(Config.BASE_URL)
    val auth = AuthManager(rest)

    suspend fun <T> step(name: String, block: suspend () -> T): T? = try {
        val r = block(); println("[OK ] $name -> $r"); r
    } catch (e: Throwable) {
        println("[ERR] $name -> ${e::class.simpleName}: ${e.message}")
        null
    }

    println("=== KMM headless smoke vs ${Config.BASE_URL} / ${Config.WS_URL} ===")

    val tokens = step("login") { auth.login(password, totp.ifBlank { null }) }
    if (tokens == null) { println("LOGIN FAILED — aborting"); return@runBlocking }
    println("    access token len=${tokens.access.length}")

    step("getFaces") { rest.getFaces().map { it.id } }
    val convs = step("getConversations") { rest.getConversations(20, 0) }
    val conv = convs?.firstOrNull()
        ?: step("createConversation") { rest.createConversation("kmm smoke", "planner-claude") }
    if (conv == null) { println("NO CONVERSATION — aborting"); return@runBlocking }
    println("    conv id=${conv.id}")
    step("getMessages") {
        rest.getMessages(conv.id, 50, null).let { "msgs=${it.messages.size} hasMore=${it.hasMore}" }
    }

    // --- WS chat (the payoff path) ---
    val ws = BoBClawWebSocket(Config.WS_URL)
    val token = auth.getAccessToken()
    if (token == null) { println("[ERR] no access token for WS"); return@runBlocking }

    val sb = StringBuilder()
    val done = CompletableDeferred<String>()
    val collector = launch {
        ws.incomingMessages.collect { msg ->
            when (msg) {
                is ServerMessage.Chunk -> { sb.append(msg.content); print(msg.content); System.out.flush() }
                is ServerMessage.MessageComplete ->
                    if (!done.isCompleted) done.complete("complete tokOut=${msg.tokensOut} ms=${msg.elapsedMs}")
                is ServerMessage.Error -> {
                    println("\n[WS-ERR] ${msg.code}: ${msg.message}")
                    if (msg.code != "decode_error" && !done.isCompleted) done.complete("error:${msg.code}")
                }
                else -> println("\n[WS] frame=${msg::class.simpleName}")
            }
        }
    }

    println("[..] connecting WS")
    ws.connect(token)
    var sent = false
    repeat(12) {
        if (!sent) {
            delay(700)
            runCatching { ws.sendMessage(conv.id, "In one sentence, what is a feature flag?", "planner-claude") }
                .onSuccess { sent = true; println("[OK ] sent message (face=planner-claude)") }
        }
    }
    if (!sent) println("[ERR] WS never connected (could not send)")

    val result = withTimeoutOrNull(90_000) { done.await() }
    println("\n[smoke] stream result: ${result ?: "TIMEOUT (no MessageComplete in 90s)"}")
    println("[smoke] final reply (${sb.length} chars): ${sb.toString().take(400)}")

    ws.disconnect()
    collector.cancel()
    println("=== smoke done ===")
}
