"""
BoBClaw Core — read-only web retrieval.

One tool, deliberately narrow: an HTTPS GET, no credentials, no POST, no cookies,
a host allow-list, private-address rejection, a size cap and a timeout.

It exists because a chat face that cannot reach a source can only describe where
a source might be — verified live 2026-10-27, when the assistant correctly refused
to invent a model-release list and said the missing capability out loud. This
gives it the smallest capability that turns "I would check Hugging Face" into a
real check.

The default allow-list is the set of primary sources for model releases plus
generic documentation hosts. Extend it without a code change with
``BOB_WEB_FETCH_HOSTS=host1,host2`` in the service environment.
"""
from __future__ import annotations

import html as _html
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
import threading

from langchain_core.tools import tool

DEFAULT_HOSTS: frozenset[str] = frozenset(
    {
        # model hubs + discovery APIs
        "huggingface.co",
        "api.github.com",
        "github.com",
        "ollama.com",
        "lmstudio.ai",
        "openrouter.ai",
        # vendor release notes
        "openai.com",
        "anthropic.com",
        "blog.google",
        "deepmind.google",
        "ai.meta.com",
        "mistral.ai",
        "x.ai",
        "api-docs.deepseek.com",
        "qwen.ai",
        "moonshot.ai",
        "z.ai",
        "minimax.io",
        "allenai.org",
        # reference docs
        "docs.python.org",
        "developer.mozilla.org",
        "en.wikipedia.org",
    }
)

MAX_BYTES = 1_000_000
MAX_CHARS = 20_000
TIMEOUT_S = 15
MAX_REDIRECTS = 3
# Search runs against a self-hosted SearXNG on loopback (the public engines
# captcha or rate-limit this host), with the DuckDuckGo lite HTML endpoint as a
# fallback when the instance is down.
SEARXNG_URL = os.environ.get("BOB_WEB_SEARCH_URL", "http://127.0.0.1:8888")
SEARCH_URL = "https://lite.duckduckgo.com/lite/?q={query}"
MAX_RESULTS = 8

# Hosts a search surfaced this turn. web_fetch may read them, which is why search
# and fetch share this module: a model that can find a source must be able to open
# it. The grant is keyed by turn id rather than held in a ContextVar, because a
# sync tool body runs in a worker thread: context values propagate *into* the
# thread but a set() inside it never propagates back out.
_turn_id: ContextVar[str] = ContextVar("web_turn_id", default="")
_turn_hosts: dict[str, set[str]] = {}
_hosts_lock = threading.Lock()


def begin_turn(turn_id: str) -> str:
    """Start a fresh grant for one turn; returns the token to reset with."""
    with _hosts_lock:
        _turn_hosts[turn_id] = set()
    return _turn_id.set(turn_id)


def end_turn(turn_id: str, token) -> None:
    with _hosts_lock:
        _turn_hosts.pop(turn_id, None)
    _turn_id.reset(token)


def granted_hosts() -> set[str]:
    with _hosts_lock:
        return set(_turn_hosts.get(_turn_id.get(), ()))
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
               "application/rss+xml", "application/atom+xml")

_TAG = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK = re.compile(r"</(?:p|div|section|article|li|tr|h[1-6]|br)\s*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


class FetchRefused(ValueError):
    """The URL is outside what this tool is allowed to retrieve."""


def allowed_hosts() -> frozenset[str]:
    extra = os.environ.get("BOB_WEB_FETCH_HOSTS", "")
    return DEFAULT_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    candidates = allowed_hosts() | granted_hosts()
    return any(host == allowed or host.endswith("." + allowed) for allowed in candidates)


def _addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise FetchRefused(f"cannot resolve {host}: {exc}") from None
    return [info[4][0] for info in infos]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url(url: str) -> str:
    """Validate one URL against the allow-list and the private-address rules."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise FetchRefused("only https URLs are retrieved")
    if not parsed.hostname:
        raise FetchRefused("URL has no host")
    if parsed.username or parsed.password:
        raise FetchRefused("credentials in a URL are refused")
    host = parsed.hostname.lower()
    if not _host_allowed(host):
        raise FetchRefused(
            f"{host} is not on the retrieval allow-list; extend it with BOB_WEB_FETCH_HOSTS"
        )
    for address in _addresses(host):
        if not _is_public(address):
            raise FetchRefused(f"{host} resolves to a non-public address")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand so each hop is re-validated."""

    def redirect_request(self, *args, **kwargs):  # noqa: D102 - stdlib signature
        return None


def _open(url: str) -> tuple[bytes, str, int]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "BoBClaw/1.0 (+read-only retrieval)",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        },
    )
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            status = int(getattr(response, "status", 0) or 0)
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location", "")
            raise _Redirect(location) from None
        raise FetchRefused(f"the source answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchRefused(f"the source could not be reached: {exc}") from None
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES]
    return body, content_type, status


class _Redirect(Exception):
    def __init__(self, location: str):
        super().__init__(location)
        self.location = location


def to_text(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    try:
        text = body.decode(charset, "replace")
    except Exception:  # noqa: BLE001 - a decode fault must not fail the tool
        text = body.decode("latin-1", "replace")
    if "html" in content_type or text.lstrip().startswith("<"):
        text = _TAG.sub(" ", text)
        text = _COMMENT.sub(" ", text)
        text = _BLOCK.sub("\n", text)
        text = _ANY_TAG.sub(" ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )
        text = _WS.sub(" ", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
    else:
        text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n[truncated at {MAX_CHARS} characters]"
    return text


@tool
def web_fetch(url: str) -> str:
    """Retrieve one https page or JSON endpoint and return its readable text.

    Read-only: GET only, no credentials, no cookies, no request body. Only hosts
    on the allow-list are retrieved, and a host that resolves to a private
    address is refused. Use it to check a primary source (a model hub listing, a
    GitHub release feed, a vendor release note) instead of describing it from
    memory.
    """
    current = str(url or "").strip()
    if not current:
        return json.dumps({"ok": False, "error": "url is required"})
    for _ in range(MAX_REDIRECTS + 1):
        try:
            check_url(current)
        except FetchRefused as exc:
            return json.dumps({"ok": False, "error": str(exc), "url": current})
        try:
            body, content_type, status = _open(current)
        except _Redirect as redirect:
            target = urllib.parse.urljoin(current, redirect.location)
            current = target
            continue
        except FetchRefused as exc:
            return json.dumps({"ok": False, "error": str(exc), "url": current})
        text = to_text(body, content_type)
        return json.dumps(
            {
                "ok": True,
                "url": current,
                "status": status,
                "content_type": content_type or "unknown",
                "bytes": len(body),
                "truncated": len(text) >= MAX_CHARS,
                "text": text,
            },
            ensure_ascii=False,
        )
    return json.dumps({"ok": False, "error": "too many redirects", "url": current})


_RESULT_ANCHOR = re.compile(
    r"<a[^>]+href=\"(?P<href>[^\"]+)\"[^>]*class=['\"]result-link['\"][^>]*>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_RESULT_SNIPPET = re.compile(
    r"class=['\"]result-snippet['\"][^>]*>(?P<body>.*?)</td>", re.IGNORECASE | re.DOTALL
)
_LINK_TEXT = re.compile(r"class=['\"]link-text['\"][^>]*>(?P<body>.*?)</span>", re.IGNORECASE | re.DOTALL)


def _clean(fragment: str) -> str:
    text = _ANY_TAG.sub(" ", fragment)
    text = _html.unescape(text.replace("&nbsp;", " "))
    return _WS.sub(" ", text).strip()


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<real url>; return the real url."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


def parse_results(html: str, limit: int = MAX_RESULTS) -> list[dict]:
    snippets = [_clean(m.group("body")) for m in _RESULT_SNIPPET.finditer(html)]
    results: list[dict] = []
    for index, match in enumerate(_RESULT_ANCHOR.finditer(html)):
        url = _unwrap(match.group("href").replace("&amp;", "&"))
        title = _clean(match.group("title"))
        host = urllib.parse.urlsplit(url).hostname or ""
        if not url.startswith("https://") or not host or not title:
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "host": host,
                "snippet": snippets[index] if index < len(snippets) else "",
            }
        )
        if len(results) >= max(1, min(limit, MAX_RESULTS)):
            break
    return results


@tool
def web_search(query: str, max_results: int = 6) -> str:
    """Search the public web; returns titles, URLs and snippets. Read-only.

    Use it to FIND sources, then read the ones that matter with web_fetch — hosts
    returned by a search in this turn are fetchable for the rest of the turn. The
    snippets are search-engine text, not evidence: quote a page only after you have
    fetched it.
    """
    text = str(query or "").strip()
    if not text:
        return json.dumps({"ok": False, "error": "query is required"})
    if len(text) > 300:
        text = text[:300]
    limit = max(1, min(int(max_results or 6), MAX_RESULTS))
    results: list[dict] = []
    errors: list[str] = []

    # 1) the local instance, JSON — structured and not subject to bot walls
    try:
        url = f"{SEARXNG_URL}/search?q={urllib.parse.quote_plus(text)}&format=json"
        body, _content_type, _status = _open(url)
        payload = json.loads(body.decode("utf-8", "replace"))
        for row in payload.get("results") or []:
            link = str(row.get("url") or "")
            host = urllib.parse.urlsplit(link).hostname or ""
            if not link.startswith("https://") or not host:
                continue
            results.append(
                {
                    "title": str(row.get("title") or "").strip(),
                    "url": link,
                    "host": host,
                    "snippet": " ".join(str(row.get("content") or "").split())[:400],
                }
            )
            if len(results) >= limit:
                break
    except (FetchRefused, ValueError, OSError) as exc:
        errors.append(f"searxng: {exc}")

    # 2) DuckDuckGo lite, HTML — used when the instance is unreachable or empty
    if not results:
        try:
            body, _content_type, _status = _open(SEARCH_URL.format(query=urllib.parse.quote_plus(text)))
            # Parse raw markup: to_text() strips the anchors this reads.
            results = parse_results(body.decode("utf-8", "replace"), limit=limit)
        except FetchRefused as exc:
            errors.append(f"duckduckgo: {exc}")

    if not results:
        return json.dumps(
            {"ok": False, "error": "the search returned no results", "detail": "; ".join(errors), "query": text},
            ensure_ascii=False,
        )
    with _hosts_lock:
        bucket = _turn_hosts.setdefault(_turn_id.get(), set())
        bucket.update(r["host"] for r in results)
    return json.dumps(
        {"ok": True, "query": text, "count": len(results), "results": results},
        ensure_ascii=False,
    )
