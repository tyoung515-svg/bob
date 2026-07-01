"""Client-IP resolution for the per-IP login lockout, rate limiter, and audit log.

By default the client is the socket peer (``request.remote``) — correct for the
loopback / direct-connection deployment. Behind a reverse proxy every request's peer
is the PROXY, so keying the lockout / rate-limit on it would treat all clients as one
(a few failed logins would lock out everyone). When ``TRUST_X_FORWARDED_FOR`` is on,
the client IP is taken from the RIGHTMOST ``X-Forwarded-For`` entry — the hop the
trusted proxy observed. Entries to its left are client-supplied and ignored, so a
client cannot spoof the header to evade the lockout.

This assumes EXACTLY ONE trusted proxy directly in front (one that sets or appends
X-Forwarded-For). Do not enable it otherwise — with a CDN or a second hop in front,
the rightmost entry is that hop, not the client.
"""
from __future__ import annotations

from aiohttp import web

from config import config


def client_ip(request: web.Request) -> str:
    if config.TRUST_X_FORWARDED_FOR:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
    return request.remote or "unknown"
