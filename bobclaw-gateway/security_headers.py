"""BoBClaw Gateway — security response headers.

Sets a conservative set of security headers on every response, including the
static web UI. The web UI is fully same-origin (Preact/htm are vendored locally,
no CDN, no inline <script>), so a strict CSP works: scripts are `'self'` only.
`style-src` allows `'unsafe-inline'` because the SPA sets element style
attributes at runtime — that does not create an script-injection surface.

For any non-loopback deployment, review this policy (and tighten `style-src` with
nonces/hashes) as part of the "before you expose to a network" checklist in
SECURITY.md.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from aiohttp import web

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)

_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def make_security_headers_middleware() -> Callable[
    [web.Request, Callable], Awaitable[web.StreamResponse]
]:
    """Build a middleware that stamps security headers on every response.

    Placed outermost so it also covers error responses returned by inner
    middleware (auth 401s, rate-limit 429s). Existing header values are not
    overwritten, so a handler can opt out of a specific header if it must.
    """

    def _stamp(resp: web.StreamResponse) -> web.StreamResponse:
        for name, value in _HEADERS.items():
            resp.headers.setdefault(name, value)
        return resp

    @web.middleware
    async def security_headers_middleware(request: web.Request, handler):
        try:
            return _stamp(await handler(request))
        except web.HTTPException as exc:
            # aiohttp raises 4xx/5xx (and redirects) as exceptions — stamp them too.
            raise _stamp(exc)

    return security_headers_middleware
