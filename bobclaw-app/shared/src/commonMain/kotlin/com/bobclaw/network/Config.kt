package com.bobclaw.network

/**
 * Single source of truth for gateway endpoints.
 *
 * The live BoBClaw gateway runs on port 7826, bound to 0.0.0.0 (IPv4 only — no IPv6).
 * Use 127.0.0.1, NOT "localhost": ktor CIO resolves localhost to ::1 first, and the WS
 * upgrade then fails with "Connection refused" against the IPv4-only listener.
 * WS path is exact (no trailing slash), scheme `ws`. See tasks/2026-06-16-kotlin-pc/GATEWAY-CONTRACT.md.
 */
object Config {
    const val BASE_URL: String = "http://127.0.0.1:7826"
    const val WS_URL: String = "ws://127.0.0.1:7826/ws/chat"
}
