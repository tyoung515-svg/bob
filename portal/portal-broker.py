#!/usr/bin/env python3
"""portal-broker.py -- LANE S: server-side stdio broker for the Bob portal.

Wire (CONTRACT.md §2, frozen):
  request  {"id":<uint>,"op":"<name>","args":{...}}
  reply    {"id":<uint>,"ok":true,"value":{...}} | {"id":<uint>,"ok":false,"error":{"code":..,"detail":..}}
  event    {"event":"<name>","data":{...}}         (no id; asynchronous, chat-scoped)

One UTF-8 JSON object per line. Request line <=256 KiB, reply line <=8 MiB
(including base64 encoding for a Builder preview of up to 5 MiB).

Python 3 standard library only. The agent bearer is read once from a 0600 file,
bound to one `sub` for the whole process life, never printed/logged/returned and
scrubbed from every outgoing line as a second line of defence. The broker speaks
only to a loopback gateway and only through the fixed endpoint map below: there is
no caller-supplied URL, no redirect, no retry, no shell, no subprocess, no
filesystem browsing, no local model fallback.

Ops are exactly CONTRACT §3. Unsupported server capabilities are reported as
explicit `gaps` in `hello`, never faked.

Ownership (CONTRACT §4). Every list reply states how ownership was established in a
machine-readable `scope`:
  * `{"mechanism":"row_owner_filter","row_owner_attributable":true}` — the rows
    carry `user_id`, the broker compared each one with the bound sub, counted
    `excluded` (different owner) and `unattributed` (no owner field, never shown);
  * `{"mechanism":"gateway_account_scope","row_owner_attributable":false}` — the
    gateway's list payload carries no owner field at all (the live `/conversations`
    and `/projects` shapes), so the broker cannot verify per row and reports no
    ownership-based exclusion count at all.
`messages.page` verifies conversation ownership itself (an owner-scoped GET) before
returning rows. One chat turn is capped by a broker-side stream budget and ends with
a typed `stream_deadline` terminal when its active work budget is exhausted.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.3.0"

# ---------------------------------------------------------------------------
# Bounds (CONTRACT §2 / brief "Bounds")
# ---------------------------------------------------------------------------
MAX_REQUEST_BYTES = 256 * 1024
# 5 MiB Builder previews expand to about 7 MiB in base64 plus JSON framing.
MAX_REPLY_BYTES = 8 * 1024 * 1024
CHUNK_TEXT_BYTES = 64 * 1024
MAX_CHAT_CONTENT_BYTES = 128 * 1024
MAX_PAGE_TEXT_BYTES = 512 * 1024
MAX_MESSAGE_METADATA_BYTES = 8 * 1024   # per row: larger metadata is replaced by a marker
MAX_MEMORY_CHARS = 12000
MAX_SEARCH_QUERY_BYTES = 4000          # gateway allows 1000 chars; refuse before it
LIMIT_CONVERSATIONS = 50
LIMIT_APPROVALS = 50
LIMIT_MESSAGES = 100
LIMIT_MEMORY_SEARCH = 50
MAX_PROJECT_DETAIL_FETCH = 25
MAX_TASK_BYTES = 60000                 # gateway: agent-services task <=60000

# Fanouts fit the client's request/reply deadlines. chat.send acknowledges its
# request ID immediately; subsequent stream events have no 145 s reply deadline
# (app/portal/shell/src-tauri/src/portal.rs, Link::call_inner). Cap active work
# separately, excluding bounded operator-question and approval waits.
FANOUT_SECONDS = 20.0                  # projects.list per-project enrichment
CATALOG_SECONDS = 25.0                 # catalog's four component calls
STREAM_SECONDS = 300.0                 # active work per turn, terminal stream_deadline
QUESTION_WAIT_SECONDS = 600.0          # core.tools.ask.DEFAULT_TIMEOUT_S

# Ownership scope mechanisms reported to the client (CONTRACT §4, S-1).
SCOPE_GATEWAY_ACCOUNT = "gateway_account_scope"   # gateway SQL scopes; rows carry no owner
SCOPE_ROW_OWNER_FILTER = "row_owner_filter"       # rows carry user_id; broker filters per row
SCOPE_PORTAL_RUN_LEDGER = "portal_run_ledger"     # broker-owned index of runs THIS portal started

# Legacy versionless runs.list only. v2 reads the core's server-owned ledger.
RUN_LEDGER_MAX_ROWS = 200
RUN_LEDGER_EXCERPT_BYTES = 160
RUN_LEDGER_ENV = "BOB_PORTAL_STATE_DIR"           # tests point this at a temp dir
RUN_LEDGER_CAVEATS = [
    "Legacy portal ledger only; use contract_version=2 for the server run list.",
    "Status is the last value the server returned to this portal, not a live poll.",
    "Receipts are what the server reported per role; they are observed, not independently verified.",
    "The ledger keeps the most recent %d rows for this owner; older rows are dropped."
    % RUN_LEDGER_MAX_ROWS,
]

PROJECT_FIELDS = ("id", "name", "description", "instructions", "default_face_id",
                  "default_backend", "conversation_count", "updated_at")
MESSAGE_FIELDS = ("id", "role", "content", "created_at", "metadata")
APPROVAL_STATUSES = {"pending", "approved", "rejected", "expired", "all"}
MEMORY_CATEGORIES = {"fact", "preference", "project", "note"}
RUN_TEAMS = {"balance-budget"}
RUN_WORKERS = {None, "bob-ds-flash", "bob-qwen-strata"}

DEFAULT_GATEWAY = "http://127.0.0.1:7836"
DEFAULT_TOKEN_FILE = os.environ.get("BOB_BROKER_TOKEN_FILE")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_JOB_RE = re.compile(r"^[a-f0-9]{32}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ATTACHMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ARTIFACT_RE = re.compile(r"^[a-z]+-[a-f0-9]{32}\.json$")
JOB_ROLES = {"researcher", "coder", "reviewer"}
MAX_JOB_TASK_BYTES = 16384             # core tool-job task cap (FILE_CAP)

# Chat error codes that end the turn (the gateway sends no terminal frame after them).
FATAL_CHAT_CODES = frozenset({
    "invalid_message", "invalid_conversation", "not_found", "face_forbidden",
    "concurrency_limit", "forbidden", "unsupported_message", "invalid_pin",
})

GAPS = [
    {"code": "no_build_request_ingress", "area": "build",
     "text": "The gateway has no build_request ingress, so the portal cannot submit a contract build."},
    {"code": "no_artifact_api", "area": "artifacts",
     "text": "Artifacts are scoped to authorized Jobs; there is no general artifact browser."},
    {"code": "no_run_list_or_dag", "area": "runs",
     "text": "Versionless runs.list is a legacy portal ledger. Negotiate execution v2 for the server run list. DAG/attach/replay are not implemented."},
    {"code": "no_structured_tool_events", "area": "chat",
     "text": "Normal chat relays no structured tool event stream; tool provenance is shown only from server message fields when present, otherwise 'not provided'."},
    {"code": "no_idempotent_send", "area": "chat",
     "text": "There is no idempotent send or resend handshake; after an unknown send outcome inspect history and never auto-retry."},
    {"code": "approval_effect_unverified", "area": "approvals",
     "text": "A recorded approval decision is not proof of effect: the row is re-read after deciding, but no effect readback exists."},
    {"code": "local_runtime_not_delivered", "area": "deployment",
     "text": "Local-runtime Bob on the PC is not delivered by this program; the local/WSL profile path is kept and labelled honestly."},
    {"code": "server_boot_version_unexposed", "area": "discovery",
     "text": "The gateway exposes neither its build version nor its boot time on the authenticated boundary; hello reports both as null."},
]

APPROVAL_GUARD_GAP = {
    "code": "approval_decision_not_permitted", "area": "approvals",
    "text": "approvals.decide is refused until the staged gateway guard for agent-token decisions is activated; enable the op with --portal-approvals once that guard is live.",
}

LOCAL_TOKEN_GAP = {
    "code": "identity_claims_read_locally", "area": "identity",
    "text": "Token identity (sub, faces, may_touch, may_not_touch, exp) is read from the token's own claims and liveness-confirmed via GET /auth/status; the gateway publishes no signed identity document.",
}

# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------
_SECRET = ""
_OWNER_SECRETS = set()  # active bearer / transient factors; memory only


def set_secret(value):
    """Register the bearer so it can never leave this process in any line."""
    global _SECRET
    _SECRET = value or ""


def scrub(text):
    if not isinstance(text, str):
        return text
    if _SECRET and _SECRET in text:
        text = text.replace(_SECRET, "[redacted]")
    for secret in tuple(_OWNER_SECRETS):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


RATE_LIMIT_MESSAGE = "Bob is handling a lot of requests right now, so this wasn't sent."


class BrokerError(Exception):
    """Typed failure. `code` is a stable slug; `detail` is short and scrubbed."""

    def __init__(self, code, detail="", retry_after=None):
        super().__init__(str(code))
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        self.code = str(code)
        self.detail = scrub(str(detail))[:240]


class GatewayRateLimited(BrokerError):
    """A marked HTTP middleware refusal, before any gateway handler ran."""

    def __init__(self, retry_after=None):
        super().__init__("rate_limited", RATE_LIMIT_MESSAGE, retry_after)


def _rate_limit_fields(exc):
    if not isinstance(exc, GatewayRateLimited):
        return {}
    fields = {"applied": False, "message": RATE_LIMIT_MESSAGE}
    if exc.retry_after is not None:
        fields["retry_after_seconds"] = exc.retry_after
    return fields


def _error_frame(rid, exc):
    payload = {"code": exc.code, **_rate_limit_fields(exc)}
    if exc.detail:
        payload["detail"] = exc.detail
    if exc.retry_after is not None:
        payload["retry_after"] = exc.retry_after
    return {"id": rid, "ok": False, "error": payload}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat().replace("+00:00", "Z")


def split_utf8(text, limit):
    """Split text into pieces of at most `limit` UTF-8 bytes, never mid-codepoint."""
    if len(text.encode("utf-8")) <= limit:
        return [text]
    pieces, buf = [], b""
    for char in text:
        encoded = char.encode("utf-8")
        if len(buf) + len(encoded) > limit:
            pieces.append(buf.decode("utf-8"))
            buf = b""
        buf += encoded
    if buf:
        pieces.append(buf.decode("utf-8"))
    return pieces


def attribute_rows(items, sub, field="user_id"):
    """Split rows by owner attribution. A missing owner field is NEVER ownership.

    Returns (kept, excluded, unattributed):
      kept         -- dict rows whose owner field equals `sub`
      excluded     -- dict rows whose owner field is present and differs, plus non-dict rows
      unattributed -- dict rows that carry no owner field at all
    Callers decide whether an unattributed row may be shown; they must never count
    it as owned by `sub` (CONTRACT §4).
    """
    kept, excluded, unattributed = [], 0, 0
    for row in items or []:
        if not isinstance(row, dict):
            excluded += 1
            continue
        if field not in row or row[field] is None:
            unattributed += 1
            continue
        if row[field] != sub:
            excluded += 1
            continue
        kept.append(row)
    return kept, excluded, unattributed


def rows_carry_owner(items, field="user_id"):
    """True when the payload actually reports an owner for at least one row."""
    return any(isinstance(row, dict) and row.get(field) is not None for row in items or [])


def scope_note(mechanism, attributable, excluded=0, unattributed=0):
    """Machine-readable statement of how ownership was established for a reply.

    The client must be able to tell `gateway_account_scope` (the gateway scoped the
    query, the rows carry no owner to check) from `row_owner_filter` (the broker
    compared every row's `user_id` with the bound sub).
    """
    return {"mechanism": mechanism,
            "row_owner_attributable": bool(attributable),
            "excluded": int(excluded),
            "unattributed": int(unattributed)}


def clamp_text(text, limit):
    """Return (text_or_prefix, truncated). Never splits a codepoint."""
    if not isinstance(text, str):
        return text, False
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    cut = data[:limit]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True


def metadata_within(metadata, limit=MAX_MESSAGE_METADATA_BYTES):
    """Return (metadata, bytes). Oversized metadata becomes an explicit marker.

    `metadata` is whatever the core wrote (tool output, inline attachment, ...) and
    is not bounded upstream: one such row would otherwise push the whole page over
    MAX_REPLY_BYTES and make the conversation's history permanently unreadable.
    Content is never silently mangled: a trimmed value says so.
    """
    if metadata is None:
        return None, 0
    try:
        raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return {"truncated": True, "bytes": None, "reason": "unserialisable"}, 0
    if len(raw) <= limit:
        return metadata, len(raw)
    return ({"truncated": True, "bytes": len(raw),
             "reason": "metadata exceeded %d bytes" % limit}, 0)


def _remaining(deadline):
    """Seconds left in an aggregate wall-clock budget (None => transport default)."""
    if deadline is None:
        return None
    return max(0.5, deadline - time.monotonic())


def _sanitize_text(text):
    """Replace unpaired surrogates with U+FFFD.

    A JSON request line may legally carry a lone surrogate escape, but such a
    character has no UTF-8 form: echoing it raises in our encoder, and escaping
    it produces a wire escape that a strict peer (the Rust bridge uses
    serde_json, which validates escapes) rejects - killing the client's link
    instead of ours. Substitution keeps the frame valid JSON for every peer and
    honestly reports the character as unknown.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _sanitize(value, depth=0):
    """Recursively make a frame safe to serialise (see _sanitize_text)."""
    if depth > 32:
        return None
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {_sanitize_text(str(k)): _sanitize(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in value]
    return value


def _printable_key(key):
    """Render a client-supplied key for an error detail, always encodable."""
    return _sanitize_text(str(key))


def _only(args, allowed):
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise BrokerError("invalid_args",
                          "unsupported argument(s): " + ", ".join(_printable_key(k) for k in unknown))


def _str_arg(args, key, *, required=False, max_bytes=200, empty_ok=False):
    if key not in args or args[key] is None:
        if required:
            raise BrokerError("invalid_args", "%s is required" % key)
        return None
    value = args[key]
    if not isinstance(value, str):
        raise BrokerError("invalid_args", "%s must be a string" % key)
    if len(value.encode("utf-8")) > max_bytes:
        raise BrokerError("invalid_args", "%s exceeds %d bytes" % (key, max_bytes))
    if not empty_ok:
        value = value.strip()
        if not value:
            raise BrokerError("invalid_args", "%s is empty" % key)
    return value


def _text_arg(args, key, *, required=True, max_bytes=MAX_CHAT_CONTENT_BYTES):
    value = _str_arg(args, key, required=required, max_bytes=max_bytes, empty_ok=True)
    if value is None:
        return None
    if not value.strip():
        raise BrokerError("invalid_args", "%s is empty" % key)
    return value


def _id_arg(args, key, *, required=True):
    value = _str_arg(args, key, required=required, max_bytes=128)
    if value is None:
        return None
    if not _ID_RE.match(value):
        raise BrokerError("invalid_args", "%s is not a valid id" % key)
    return value


def _int_arg(args, key, *, default=None, minimum=0, maximum=10 ** 9):
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if isinstance(value, str) and maximum == 9223372036854775807:
        if not re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
            raise BrokerError("invalid_args", "%s must be a canonical decimal integer" % key)
        value = int(value)
    elif type(value) is int and value > 9007199254740991:
        raise BrokerError("invalid_args", "%s requires a decimal string above the JavaScript safe range" % key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerError("invalid_args", "%s must be an integer" % key)
    if value < minimum or value > maximum:
        raise BrokerError("invalid_args", "%s must be between %d and %d" % (key, minimum, maximum))
    return value


def _limit_arg(args, key, *, default, maximum):
    # The refusal names the op's own bound; a value above it is clamped, not refused.
    if key not in args or args[key] is None:
        return default
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerError("invalid_args", "%s must be an integer" % key)
    if value < 1:
        raise BrokerError("invalid_args", "%s must be between 1 and %d" % (key, maximum))
    return min(value, maximum)


def _bool_arg(args, key):
    if key not in args or args[key] is None:
        return False
    if not isinstance(args[key], bool):
        raise BrokerError("invalid_args", "%s must be a boolean" % key)
    return args[key]


def _now_ts():
    return time.time()


# ---------------------------------------------------------------------------
# HTTP / WebSocket transport (stdlib only)
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _error_detail(exc, raw=None):
    if raw is None:
        try:
            raw = exc.read(4096)
        except Exception:  # noqa: BLE001 - best effort only
            return ""
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:200]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("code") or err.get("message") or ""
            if not detail:
                detail = json.dumps(err, ensure_ascii=False)
            return str(detail)[:200]
        if isinstance(err, str):
            return err[:200]
    return text[:200]


def _http_error(exc):
    code = int(getattr(exc, "code", 0) or 0)
    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
    retry = int(retry) if retry.isdecimal() and len(retry) <= 5 else None
    try:
        raw = exc.read(4097)
    except Exception:  # noqa: BLE001 - preserve the HTTP status even without a body
        raw = b""
    try:
        body = json.loads(raw) if len(raw) <= 4096 else None
    except (ValueError, UnicodeDecodeError):
        body = None
    # Only our pre-handler middleware's top-level signature proves non-application.
    # Provider/nested errors and unmarked HTTP 429s retain their previous semantics.
    if (code == 429 and isinstance(body, dict) and body.get("code") == "rate_limited"
            and body.get("error") == "rate limit exceeded"):
        seconds = body.get("retry_after_seconds")
        if type(seconds) is int and 1 <= seconds <= 86400:
            retry = seconds
        return GatewayRateLimited(retry_after=retry)
    return BrokerError("gateway_http_%d" % code, _error_detail(exc, raw[:4096]), retry)


class LiveTransport:
    """Loopback HTTP + RFC6455 WebSocket. One attempt per call; never retried."""

    def __init__(self, base, token, *, timeout=30.0, chat_idle=900.0, token_file=None, owner_headers=None):
        self.base = base.rstrip("/")
        self._token = token
        self._token_file = token_file
        self.timeout = float(timeout)
        self.chat_idle = float(chat_idle)
        self._owner_headers = owner_headers or {}

    def _bearer(self):
        # The root timer atomically replaces broker.token. Read it for each new
        # request/upgrade so long-lived stdio brokers pick up renewal as well.
        # A renewed token may never change identity or cross a recovery epoch.
        if self._token_file:
            token = load_token(self._token_file)
            if token != self._token:
                old, new = decode_claims(self._token), decode_claims(token)
                if (old.get("sub") != new.get("sub")
                        or new.get("token_type") not in {"agent", "portal"}
                        or (old.get("token_type") == "portal" and new.get("token_type") != "portal")):
                    raise BrokerError("identity_mismatch", "renewed token identity differs")
                if old.get('service_epoch', 0) != new.get('service_epoch', 0):
                    raise BrokerError('credential_epoch_changed', 'Restart the broker after receipt reconciliation')
                self._token = token
                set_secret(token)
        return self._token

    # -- REST ---------------------------------------------------------------
    def rest(self, method, path, params=None, body=None, timeout=None, headers=None, owner_headers=None):
        url = self.base + path
        budget = self.timeout if timeout is None else max(0.5, min(self.timeout, float(timeout)))
        if params:
            pairs = [(k, v) for k, v in params.items() if v is not None]
            if pairs:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        extra = headers or {}
        headers = {"Authorization": "Bearer " + self._bearer(), "Accept": "application/json"}
        for key, value in {**self._owner_headers, **(owner_headers or {})}.items():
            if key not in {"X-Bob-Broker-Link", "X-Bob-Portal-Credential"}:
                raise BrokerError("invalid_owner_header")
            headers[key] = value
        for name, value in extra.items():
            # Only the fixed idempotency header may be added; never auth/host overrides.
            if name != "Idempotency-Key" or not _KEY_RE.match(str(value)):
                raise BrokerError("invalid_args", "unsupported request header")
            headers[name] = str(value)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=budget) as response:
                raw = response.read(MAX_REPLY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise BrokerError("transport_unavailable", type(exc).__name__) from None
        if len(raw) > MAX_REPLY_BYTES:
            raise BrokerError("response_oversize", "gateway reply exceeded %d bytes" % MAX_REPLY_BYTES)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BrokerError("invalid_json", "gateway returned a non-JSON body") from None

    # -- WebSocket ----------------------------------------------------------
    def ws_connect(self, path, headers=None, idle=None):
        parts = urllib.parse.urlsplit(self.base)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        merged = {"Authorization": "Bearer " + self._bearer()}
        merged.update(headers or {})
        return RawWebSocket(host, port, path, merged,
                            timeout=self.timeout, idle=idle or self.chat_idle)


class RawWebSocket:
    """Minimal RFC6455 client: masked client frames, unmasked server frames,
    fragmented messages, ping/pong, close. Loopback only, so no TLS."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host, port, path, headers, *, timeout=30.0, idle=900.0,
                 max_frame=8 * 1024 * 1024):
        self.max_frame = max_frame
        self._buffer = b""
        self._send_lock = threading.Lock()
        self._fragments = []
        self._fragment_opcode = None
        self._closed = False
        self.read_deadline = None
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise BrokerError("transport_unavailable", "websocket connect failed: %s" % type(exc).__name__) from None
        try:
            self._handshake(host, port, path, headers)
            self.sock.settimeout(idle)
        except Exception:
            self._hard_close()
            raise

    # -- handshake ----------------------------------------------------------
    def _handshake(self, host, port, path, headers):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s:%d" % (host, port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: " + key,
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in headers.items():
            lines.append("%s: %s" % (name, value))
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        try:
            self.sock.sendall(request)
        except OSError:
            raise BrokerError("transport_unavailable", "websocket upgrade write failed") from None
        head = b""
        while b"\r\n\r\n" not in head:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise BrokerError("transport_timeout", "websocket upgrade timed out") from None
            except OSError:
                raise BrokerError("transport_unavailable", "websocket upgrade read failed") from None
            if not chunk:
                raise BrokerError("transport_unavailable", "websocket upgrade closed early")
            head += chunk
            if len(head) > 65536:
                raise BrokerError("invalid_response", "websocket upgrade header too large")
        header_block, _, rest = head.partition(b"\r\n\r\n")
        self._buffer = rest
        text = header_block.decode("latin-1")
        status_line = text.split("\r\n", 1)[0]
        fields = status_line.split(" ")
        if len(fields) < 2 or fields[1] != "101":
            code = fields[1] if len(fields) > 1 and fields[1].isdigit() else "000"
            raise BrokerError("gateway_http_%s" % code, "websocket upgrade refused")
        received = {}
        for line in text.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            received[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        if received.get("sec-websocket-accept") != expected:
            raise BrokerError("invalid_response", "websocket accept key mismatch")

    # -- frames -------------------------------------------------------------
    def _read_exact(self, count):
        while True:
            if self.read_deadline is not None:
                remaining = self.read_deadline - time.monotonic()
                if remaining <= 0:
                    raise BrokerError("transport_timeout", "websocket read deadline reached")
                self.sock.settimeout(remaining)
            if len(self._buffer) >= count:
                break
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise BrokerError("transport_timeout", "websocket read timed out") from None
            except OSError:
                raise BrokerError("transport_unavailable", "websocket read failed") from None
            if not chunk:
                raise BrokerError("transport_unavailable", "websocket closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def recv(self):
        """Return ('text'|'binary'|'close', payload: bytes, close_code|None)."""
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > self.max_frame:
                raise BrokerError("response_oversize", "websocket frame %d bytes" % length)
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                return ("close", b"", code)
            if opcode == 0x0:  # continuation
                if self._fragment_opcode is None:
                    raise BrokerError("invalid_response", "unexpected websocket continuation")
                self._fragments.append(payload)
                if fin:
                    data = b"".join(self._fragments)
                    kind = "text" if self._fragment_opcode == 0x1 else "binary"
                    self._fragments, self._fragment_opcode = [], None
                    return (kind, data, None)
                continue
            if opcode in (0x1, 0x2):
                if not fin:
                    self._fragment_opcode = opcode
                    self._fragments = [payload]
                    continue
                return ("text" if opcode == 0x1 else "binary", payload, None)
            raise BrokerError("invalid_response", "unsupported websocket opcode %d" % opcode)

    def _send_frame(self, opcode, payload):
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        with self._send_lock:
            if self._closed:
                raise BrokerError("stream_closed", "websocket is closed")
            try:
                self.sock.sendall(bytes(header) + masked)
            except OSError:
                raise BrokerError("transport_unavailable", "websocket write failed") from None

    def send_text(self, text):
        data = text.encode("utf-8")
        if len(data) > MAX_REPLY_BYTES:
            raise BrokerError("request_oversize", "websocket text frame too large")
        self._send_frame(0x1, data)

    def send_json(self, payload):
        self.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def close(self):
        if self._closed:
            return
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except BrokerError:
            pass
        self._hard_close()

    def _hard_close(self):
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Output sink
# ---------------------------------------------------------------------------
class JsonLineSink:
    """Serialises frames to one JSON line each, scrubbing the bearer and enforcing
    the 1 MiB reply bound."""

    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def _write(self, obj):
        # Surrogates are substituted first (R7-R1): an escaped surrogate would be
        # valid JSON here but refused by the strict Rust client, and an unescaped
        # one cannot be encoded at all. After this the dump always encodes.
        try:
            clean = _sanitize(obj)
            text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise BrokerError("internal_error", "reply frame is not serialisable")
        text = scrub(text)
        data = (text + "\n").encode("utf-8", "replace")
        if len(data) > MAX_REPLY_BYTES:
            raise BrokerError("response_oversize", "reply exceeded %d bytes" % MAX_REPLY_BYTES)
        with self.lock:
            self.stream.write(data)
            self.stream.flush()

    def reply(self, frame):
        self._write(frame)

    def event(self, name, data):
        try:
            self._write({"event": name, "data": data})
        except BrokerError:
            self._write({"event": "error",
                         "data": {"message": "event exceeded the reply bound", "code": "response_oversize"}})


class HandlerResult:
    __slots__ = ("value", "release")

    def __init__(self, value, release=None):
        self.value = value
        self.release = release


# ---------------------------------------------------------------------------
# Chat job: one WebSocket per chat.send
# ---------------------------------------------------------------------------
class ChatJob:
    def __init__(self, broker, request_id, conversation_id, content, pins, research):
        self.broker = broker
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.content = content
        self.requested = pins
        self.research = research
        self.ws = None
        self.gate = threading.Event()
        self.finished = threading.Event()
        self.deadline = None
        self.budget = 0.0
        self.stop_requested = False
        self.waiting_until = None
        self.question_until = None
        self.clock_tick = None
        self._stop_lock = threading.Lock()

    # -- helpers ------------------------------------------------------------
    def _emit(self, name, data):
        self.broker.emit(name, {**data, "request_id": str(self.request_id),
                               "conversation_id": str(self.conversation_id)})

    def _send(self, frame):
        if self.ws is None:
            raise BrokerError("stream_closed", "no websocket for this turn")
        self.ws.send_json(frame)

    @staticmethod
    def _decode(payload):
        try:
            event = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return event if isinstance(event, dict) else None

    def _switch(self, frame, expected):
        self._send(frame)
        for _ in range(64):
            kind, payload, close_code = self.ws.recv()
            if kind == "close":
                raise BrokerError("transport_unavailable",
                                  "socket closed during pin switch (code %s)" % close_code)
            event = self._decode(payload)
            if event is None:
                continue
            kind_name = event.get("type")
            if kind_name == expected:
                return event
            if kind_name == "error":
                raise BrokerError(str(event.get("code") or "pin_rejected"),
                                  str(event.get("message") or "pin rejected"))
        raise BrokerError("pin_ack_missing", "no %s acknowledgment" % expected)

    # -- stop ---------------------------------------------------------------
    def request_stop(self):
        with self._stop_lock:
            if self.finished.is_set():
                return False
            # Recorded unconditionally: a stop that arrives while this job is
            # still doing its pre-socket REST checks must still stop the turn.
            # The old version returned False with ws=None and let `_run` send the
            # message frame anyway - the UI was told "no active generation"
            # about a turn that then ran to completion and persisted.
            self.stop_requested = True
            if self.ws is None:
                return True
            try:
                self.ws.send_json({"type": "stop_generation",
                                    "conversation_id": self.conversation_id})
            except BrokerError:
                # The flag still ends the turn before the message frame.
                return True
            return True

    # -- run ----------------------------------------------------------------
    def run(self):
        try:
            self._run()
        except BrokerError as exc:
            self._emit("error", {"message": exc.detail or exc.code, "code": exc.code,
                                 **_rate_limit_fields(exc)})
            self._terminal(exc.code)
        except Exception as exc:  # noqa: BLE001 - never leak internals to the client
            self._emit("error", {"message": "internal broker failure", "code": "internal_error"})
            self._terminal("internal_error")
            _ = exc
        finally:
            self.finished.set()
            if self.ws is not None:
                self.ws.close()
            self.broker.forget(self)

    def _run(self):
        # Read the persisted pins first: never open a socket for a conversation this
        # principal cannot read, and never touch the gateway before that check.
        pins = dict(self.broker.conversation_pins(self.conversation_id))
        if self.stop_requested:
            # Stopped before the socket was even opened: dispatch nothing.
            self._terminal("stop_requested")
            return
        # Active work has its own budget; chat.send's acknowledgement has already
        # released the client's request/reply channel. Waits are bounded separately.
        self.budget = max(0.0, float(self.broker.stream_seconds))
        self.deadline = time.monotonic() + self.budget
        try:
            self.ws = self.broker.transport.ws_connect(
                "/ws/chat", idle=min(float(self.broker.chat_idle), max(0.05, self.budget)))
        except BrokerError as exc:
            self._emit("error", {"message": exc.detail or exc.code, "code": exc.code,
                                 **_rate_limit_fields(exc)})
            self._terminal(exc.code)
            return
        face = self.requested.get("face_id")
        backend = self.requested.get("backend")
        model = self.requested.get("model")
        profile = self.requested.get("profile")
        locale = self.requested.get("locale")

        if face is not None:
            ack = self._switch({"type": "switch_face", "conversation_id": self.conversation_id,
                                "face_id": face}, "face_switched")
            pins["face_id"] = ack.get("face_id")
        if backend is not None:
            ack = self._switch({"type": "switch_model", "conversation_id": self.conversation_id,
                                "backend": backend, "model": model or ""}, "model_switched")
            pins["backend"] = ack.get("backend")
            pins["model"] = ack.get("model")
        if profile is not None:
            ack = self._switch({"type": "switch_profile", "conversation_id": self.conversation_id,
                                "profile": profile}, "profile_switched")
            pins["profile"] = ack.get("profile")
        if locale is not None:
            ack = self._switch({"type": "switch_locale", "conversation_id": self.conversation_id,
                                "locale": locale}, "locale_switched")
            pins["locale"] = ack.get("locale")

        self._emit("pins", {"conversation_id": self.conversation_id,
                            "face_id": pins.get("face_id"),
                            "backend": pins.get("backend"),
                            "model": pins.get("model"),
                            "profile": pins.get("profile"),
                            "locale": pins.get("locale")})

        frame = {"type": "message", "conversation_id": self.conversation_id,
                 "content": self.content}
        # The turn id travels upstream so every stored row of this turn can be
        # matched to it (an answer split by a question is several rows).
        if self.request_id:
            frame["request_id"] = str(self.request_id)
        if self.research:
            frame["research"] = True
        # Check and send under the stop lock: a stop landing between the two
        # would otherwise be accepted and then ignored (R7-R2).
        dispatched = False
        with self._stop_lock:
            if self.stop_requested:
                stop_now = True
            else:
                stop_now = False
                # Deliberately no `backend` key: the backend is the server-side pin above.
                self._send(frame)
                dispatched = True
        if stop_now:
            self._terminal("stop_requested")
            return
        _ = dispatched
        self._read_loop()

    def _advance_clock(self):
        tick = time.monotonic()
        if self.waiting_until is not None or self.question_until is not None:
            self.deadline += tick - self.clock_tick
        self.clock_tick = tick
        for expiry, code in ((self.question_until, "question_unanswered"),
                             (self.waiting_until, "approval_expired")):
            if expiry is not None and tick >= expiry:
                self.request_stop()
                self._terminal(code)
                return False
        if self.deadline is not None and tick >= self.deadline:
            self.request_stop()
            self._terminal("stream_deadline")
            return False
        return True

    def _read_loop(self):
        self.clock_tick = time.monotonic()
        while self._advance_clock():
            # Also bound a silent socket or a stream of ping/pong frames: neither
            # is evidence that the operator answered or that a wait may extend.
            waits = [expiry for expiry in (self.waiting_until, self.question_until)
                     if expiry is not None]
            self.ws.read_deadline = min(waits) if waits else self.deadline
            try:
                kind, payload, close_code = self.ws.recv()
            except BrokerError as exc:
                if exc.code == "transport_timeout" and not self._advance_clock():
                    return
                raise
            if not self._advance_clock():
                return
            if kind == "close":
                self._terminal("connection_closed")
                return
            event = self._decode(payload)
            if event is None:
                continue
            frame_type = event.get("type")
            if frame_type == "builder_wait":
                expiry = event.get("expires_at")
                if event.get("waiting") is True and isinstance(expiry,(int,float)) and not isinstance(expiry,bool):
                    expiry = time.monotonic() + max(0.0, min(expiry-time.time(), 600))
                    self.waiting_until = min(self.waiting_until,expiry) if self.waiting_until is not None else expiry
                else:
                    self.waiting_until = None
                self._emit("builder_wait", {"request_id":self.request_id,
                    "agent":event.get("agent"),
                    "approval_id":event.get("approval_id"),"waiting":self.waiting_until is not None,
                    "expires_at":(time.time()+self.waiting_until-time.monotonic())
                    if self.waiting_until is not None else None})
            elif frame_type == "chunk":
                content = event.get("content")
                if not isinstance(content, str) or not content:
                    continue
                for piece in split_utf8(content, CHUNK_TEXT_BYTES):
                    self._emit("chunk", {"request_id": self.request_id, "content": piece,
                                         "model": event.get("model"),
                                         "backend": event.get("backend")})
            elif frame_type == "question":
                # S2 (2026-10-27): the turn is parked in core until the operator
                # answers. The portal renders this as a question card.
                if self.question_until is None:
                    self.question_until = time.monotonic() + QUESTION_WAIT_SECONDS
                self._emit("question", {
                    "request_id": self.request_id,
                    "question_id": event.get("question_id"),
                    "question": event.get("question"),
                    **({"origin": event["origin"]} if event.get("origin") else {}),
                    "choices": event.get("choices") or [],
                })
            elif frame_type == "tool_activity":
                # execute.py emits this as soon as ask_operator returns, including
                # after an answer delivered through another portal connection.
                if event.get("tool") == "ask_operator" and event.get("phase") == "result":
                    self.question_until = None
                # S1 (2026-10-27): what the turn is doing, as it does it. Bounded
                # previews only; the portal renders them as an activity trail.
                self._emit("tool_activity", {
                    "request_id": self.request_id,
                    "phase": event.get("phase"),
                    "tool": event.get("tool"),
                    "args": event.get("args"),
                    "preview": event.get("preview"),
                    "ok": event.get("ok"),
                    "elapsed_ms": event.get("elapsed_ms"),
                })
            elif frame_type == "approval_request":
                self._emit("approval_request", {"approval_id": event.get("approval_id"),
                                                 "action": event.get("action"),
                                                 "details": event.get("details")})
            elif frame_type == "error":
                code = str(event.get("code") or "upstream_error")
                reason = event.get("reason")
                if code == "concurrency_limit":
                    reason = "too_many_active_conversations"
                self._emit("error", {"message": str(event.get("message") or "")[:500],
                                     "code": code, **({"reason": reason} if reason else {})})
                if code in FATAL_CHAT_CODES:
                    self._terminal(code, reason=reason)
                    return
            elif frame_type == "message_complete":
                self._emit("complete", {
                    "request_id": self.request_id,
                    "message_id": event.get("message_id"),
                    "tokens_in": event.get("tokens_in"),
                    "tokens_out": event.get("tokens_out"),
                    "elapsed_ms": event.get("elapsed_ms"),
                    **{key: event[key] for key in ("pre_question_message_ids", "stopped_reason", "step_extensions",
                                                             "model", "backend", "requested_model", "provider_reported_model", "model_usage")
                       if event.get(key) or (key == "step_extensions" and key in event)},
                })
                return
            elif frame_type == "generation_stopped":
                self._terminal(str(event.get("code") or "stopped"))
                return
            # Unknown frame types (council_*, switch echoes) are ignored: the portal
            # never opts into council events, and the contract lists no other events.

    def _terminal(self, code, reason=None):
        if self.finished.is_set():
            return
        self._emit("stopped", {"request_id": self.request_id, "code": code,
                               **({"reason": reason} if reason else {})})
        self.finished.set()


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------
def _ledger_dir():
    override = os.environ.get(RUN_LEDGER_ENV)
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "bob-portal")


def _ledger_row(row, team=None, worker=None, previous=None):
    """Project one server run row into a ledger row. Nothing is invented: a field the
    server did not report stays as the previously recorded value or null."""
    base = previous or {}
    receipts = []
    for entry in _as_list(row.get("receipts")):
        if not isinstance(entry, dict):
            continue
        receipts.append({
            "role": entry.get("role"),
            "model_face": entry.get("model_face") or entry.get("seat"),
            "model_pin": entry.get("model_pin"),
            "elapsed_seconds": entry.get("elapsed_seconds", entry.get("elapsed")),
        })
    excerpt = base.get("task_excerpt")
    task = row.get("task")
    if isinstance(task, str) and task:
        excerpt = task.encode("utf-8")[:RUN_LEDGER_EXCERPT_BYTES].decode("utf-8", "ignore")
    now = _now_iso()
    # Prefer the server's own creation time; the portal clock is only a fallback
    # and the row says which one it used.
    started_at = base.get("started_at")
    source = base.get("started_at_source")
    created = row.get("created_at")
    if isinstance(created, (int, float)) and not isinstance(created, bool) and source != "server_created_at":
        try:
            started_at = _epoch_iso(created)
            source = "server_created_at"
        except (OverflowError, OSError, ValueError):
            pass
    if not started_at:
        started_at, source = now, "portal_clock"
    return {
        "run_id": str(row.get("id") or base.get("run_id") or ""),
        "team": row.get("team") or team or base.get("team"),
        "worker": worker or base.get("worker"),
        "task_excerpt": excerpt,
        "started_at": started_at,
        "started_at_source": source or "portal_clock",
        "last_status": str(row.get("status") or base.get("last_status") or "unknown"),
        "last_checked_at": now,
        "receipts": receipts or list(base.get("receipts") or []),
        "source": "portal_run_ledger",
    }


class RunLedger:
    """Durable, owner-scoped index of the runs THIS portal started.

    The gateway persists run rows but publishes no run list, so a client's only
    durable run view is what this broker recorded when it dispatched.
    Fixed path per bound subject under a 0700 directory, file 0600, exclusive flock
    for writes and shared for reads, bounded to RUN_LEDGER_MAX_ROWS. There is no
    caller-supplied path and no browsing.

    A broken ledger must never fail a run dispatch: reads raise a typed error the
    caller reports, and a write over a corrupt file moves the broken bytes aside
    under a unique name (`*.corrupt-<uuid>`) instead of overwriting them. Writes go
    to a temporary file and are renamed into place, so a killed process or a short
    write leaves the previous ledger intact.
    """

    def __init__(self, sub):
        self.sub = str(sub)
        self.safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.sub)[:64] or "unknown"

    @property
    def path(self):
        return os.path.join(_ledger_dir(), "runs-%s.json" % self.safe)

    def _directory(self, create):
        directory = os.path.dirname(self.path)
        if create:
            try:
                os.makedirs(directory, mode=0o700, exist_ok=True)
            except OSError:
                raise BrokerError("run_ledger_unavailable",
                                  "the portal state directory is not usable") from None
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            if create:
                raise BrokerError("run_ledger_unavailable",
                                  "the portal state directory is not usable") from None
            return None
        except OSError:
            raise BrokerError("run_ledger_unavailable",
                              "the portal state directory is not usable") from None
        if stat.S_ISLNK(info.st_mode):
            raise BrokerError("run_ledger_insecure", "the portal state directory is a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise BrokerError("run_ledger_unavailable", "the portal state path is not a directory")
        if info.st_uid != os.getuid():
            raise BrokerError("run_ledger_insecure", "the portal state directory is not owned by this user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            # makedirs() mode is umask-filtered and only applies to a new leaf, so an
            # existing directory is re-tightened here instead of being trusted.
            try:
                os.chmod(directory, 0o700)
            except OSError:
                raise BrokerError("run_ledger_insecure",
                                  "the portal state directory could not be locked down") from None
        return directory

    def _lock(self, exclusive):
        """Hold flock on a SIDECAR file, never on the ledger itself: the ledger is
        replaced by rename on every write, which would let a second process lock a
        stale inode and rewrite from an empty list."""
        if self._directory(create=exclusive) is None:
            return None
        try:
            fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise BrokerError("run_ledger_unavailable", "the run ledger lock file could not be opened") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError:
            os.close(fd)
            raise BrokerError("run_ledger_io", "the run ledger lock could not be taken") from None
        return fd

    def _load(self):
        if self._directory(create=False) is None:
            return []
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        except OSError:
            raise BrokerError("run_ledger_insecure", "the run ledger is not readable as a plain file") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BrokerError("run_ledger_insecure", "the run ledger is not a regular file")
            # Defence in depth: a ledger created before the 0700 directory existed (or
            # replaced by hand) is re-tightened on read, not trusted at 0644. Owner bits
            # are preserved, so a deliberately read-only ledger (0444) stays read-only.
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode & 0o077:
                try:
                    os.fchmod(fd, mode & 0o700)
                except OSError:
                    pass
            chunks = []
            while True:
                try:
                    block = os.read(fd, 65536)
                except OSError:
                    raise BrokerError("run_ledger_io", "the run ledger could not be read") from None
                if not block:
                    break
                chunks.append(block)
        except OSError:
            raise BrokerError("run_ledger_io", "the run ledger could not be read") from None
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        if not raw.strip():
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BrokerError("run_ledger_corrupt", "the run ledger is not readable JSON") from None
        rows = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise BrokerError("run_ledger_corrupt", "the run ledger has no runs list")
        return [row for row in rows if isinstance(row, dict)]

    def _save(self, rows):
        """Atomic replace: a short write or a killed process leaves the previous
        ledger intact instead of truncating the recorded history."""
        directory = self._directory(create=True)
        payload = {"version": 1, "owner": self.sub, "runs": rows[:RUN_LEDGER_MAX_ROWS]}
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._sweep_stale_temps()
        tmp = "%s.tmp.%d.%s" % (self.path, os.getpid(), uuid.uuid4().hex[:8])
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise BrokerError("run_ledger_unavailable",
                              "a temporary run ledger file could not be created") from None
        try:
            written = 0
            while written < len(blob):
                written += os.write(fd, blob[written:])
            os.fsync(fd)
        except OSError:
            try:
                os.close(fd)
            finally:
                fd = -1
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise BrokerError("run_ledger_io", "the run ledger could not be written") from None
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise BrokerError("run_ledger_io", "the run ledger could not be replaced atomically") from None
        # The rename is committed at this point. A failing directory fsync only weakens
        # crash durability of that rename; reporting it as a failed write would tell the
        # client "not recorded" while the row IS recorded.
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _sweep_stale_temps(self):
        """A SIGKILL between create and rename leaves a *.tmp.* file. Only files
        older than an hour are removed, so a save in flight in another process is
        never touched."""
        directory = os.path.dirname(self.path)
        prefix = os.path.basename(self.path) + ".tmp."
        cutoff = time.time() - 3600
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not name.startswith(prefix):
                continue
            candidate = os.path.join(directory, name)
            try:
                if os.lstat(candidate).st_mtime < cutoff:
                    os.unlink(candidate)
            except OSError:
                continue

    def _set_aside(self):
        aside = "%s.corrupt-%s" % (self.path, uuid.uuid4().hex[:12])
        try:
            os.replace(self.path, aside)
        except FileNotFoundError:
            return
        except OSError:
            raise BrokerError("run_ledger_unavailable",
                              "a corrupt run ledger could not be moved aside") from None

    def record(self, row):
        fd = self._lock(exclusive=True)
        if fd is None:
            raise BrokerError("run_ledger_unavailable", "the portal state directory is not usable")
        try:
            try:
                rows = self._load()
            except BrokerError as exc:
                if exc.code != "run_ledger_corrupt":
                    raise
                # The broken bytes are preserved under a unique name; recording
                # continues from an empty list instead of overwriting them.
                self._set_aside()
                rows = []
            # Update in place when the run is already recorded, so the list stays ordered
            # by FIRST dispatch (newest first) instead of jumping a refreshed old run to
            # the top; a refreshed run only changes its status/receipts. Any duplicate id
            # in the file (only reachable through a hand-edited or restored ledger) is
            # collapsed to its first occurrence, so "one row per run_id" holds after any
            # write, not just for the id being recorded.
            kept = []
            seen = set()
            replaced = False
            for existing in rows:
                key = existing.get("run_id")
                if key == row.get("run_id"):
                    if not replaced:
                        kept.append(row)
                        replaced = True
                    continue
                if key is None:
                    kept.append(existing)      # unidentifiable rows are never merged
                    continue
                if key in seen:
                    continue
                seen.add(key)
                kept.append(existing)
            if not replaced:
                kept.insert(0, row)
            rows = kept
            self._save(rows)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def rows(self):
        fd = self._lock(exclusive=False)
        if fd is None:
            return []
        try:
            return self._load()
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


WORKFLOW_FIELDS = {
    'catalog':(), 'list':('cursor','limit'), 'get':('id','revision'),
    'save':('id','definition','expected_revision','request_key'),
    'copy':('id','revision','name','request_key'), 'delete':('id','expected_revision','request_key'),
    'propose':('definition_ref','definition','inputs','bindings','project_id','workspace','origin_conversation_id','expires_at','run_mode','request_key'),
    'proposal':('proposal_id',), 'proposals':('cursor','limit','include_expired'),
    'propose_again':('proposal_id',), 'grant_start':('proposal_id','proposal_revision','proposal_digest','allow_same_model_review','request_key'),
    'runs':('cursor','limit','project_id','status'), 'run':('run_id','stage_cursor'),
    'events':('run_id','after_seq','limit','wait'), 'pause':('run_id','expected_revision','request_key'),
    'resume':('run_id','expected_revision','wait_id','request_key'), 'stop':('run_id','request_key'),
    'message':('run_id','stage_key','text','request_key'), 'gate':('run_id','wait_id','decision','expected_revision','request_key'),
    'artifact':('run_id','artifact_id','offset','max_bytes'),
}


class Broker:
    def __init__(self, transport, identity, sink, *, allow_approvals=False, spawn=None,
                 server=None, chat_idle=900.0, fanout_seconds=FANOUT_SECONDS,
                 catalog_seconds=CATALOG_SECONDS, stream_seconds=STREAM_SECONDS):
        self.transport = transport
        self._owner_transport = None
        self._owner_session = {"active": False}
        self._owner_eligible = None
        self._owner_link_nonce = uuid.uuid4().hex + uuid.uuid4().hex
        self.identity = identity
        self.sub = identity["sub"]
        self.sink = sink
        self.allow_approvals = bool(allow_approvals)
        self._spawn = spawn
        self._gated = spawn is not None
        self.server = server or {"service": None, "status": None, "boot": None, "version": None}
        self.chat_idle = chat_idle
        self.fanout_seconds = float(fanout_seconds)
        self.catalog_seconds = float(catalog_seconds)
        self.stream_seconds = float(stream_seconds)
        self._workflow_watches = {}
        self._workflow_watch_lock = threading.Lock()
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self.ledger = RunLedger(self.sub)
        self._handlers = {
            "hello": self.op_hello,
            "ping": self.op_ping,
            "catalog": self.op_catalog,
            "conversations.list": self.op_conversations_list,
            "conversations.create": self.op_conversations_create,
            "conversations.get": self.op_conversations_get,
            "conversations.rename": self.op_conversations_rename,
            "conversations.assign_project": self.op_conversations_assign_project,
            "messages.page": self.op_messages_page,
            "chat.send": self.op_chat_send,
            "chat.stop": self.op_chat_stop,
            "chat.answer": self.op_chat_answer,
            "chat.pins": self.op_chat_pins,
            "approvals.list": self.op_approvals_list,
            "approvals.get": self.op_approvals_get,
            "approvals.decide": self.op_approvals_decide,
            "projects.list": self.op_projects_list,
            "projects.create": self.op_projects_create,
            "projects.update": self.op_projects_update,
            "projects.delete": self.op_projects_delete,
            "memory.list": self.op_memory_list,
            "files.roots": self.op_files_roots,
            "files.list": self.op_files_list,
            "files.read": self.op_files_read,
            "memory.status": self.op_memory_status,
            "memory.search": self.op_memory_search,
            "memory.read": self.op_memory_read,
            "memory.save": self.op_memory_save,
            "runs.start": self.op_runs_start,
            "teams.list": self.op_teams_list,
            "teams.save": self.op_teams_save,
            "runs.list": self.op_runs_list,
            "runs.get": self.op_runs_get,
            "runs.cancel": self.op_runs_cancel,
            "runs.resume": self.op_runs_resume,
            "flags.list": self.op_flags_list,
            "flags.get": self.op_flags_get,
            "flags.resolve": self.op_flags_resolve,
            "budgets.set": self.op_budgets_set,
            "budgets.get": lambda args: self._owner_read_operation("budgets.get", args, "project_id", "/agent-services/budgets/projects/"),
            "budgets.list": lambda args: self._owner_read_operation("budgets.list", args, None, "/agent-services/budgets"),
            "budgets.remove": lambda args: self._owner_mutation("budgets.remove", args, "project_id", "/agent-services/budgets/projects/", "/remove"),
            "grants.prepare": self.op_grants_prepare,
            "workspace.browse": self.op_workspace_browse,
            **{"pc."+op:(lambda args,op=op:self.op_pc(op,args)) for op in ("pending","claim","receipt","end")},
            **{"builder." + op: (lambda args, op=op: self.op_builder(op, args))
               for op in ("mode.get", "mode.set", "approval.get", "approve", "preview", "network.set", "network_log", "task.end", "project.result")},
            "grants.issue": self.op_grants_issue,
            "grants.list": lambda args: self._owner_read_operation("grants.list", args, None, "/agent-services/grants"),
            "grants.get": lambda args: self._owner_read_operation("grants.get", args, "grant_id", "/agent-services/grants/"),
            "grants.revoke": lambda args: self._owner_mutation("grants.revoke", args, "grant_id", "/agent-services/grants/", "/revoke"),
            "owner.begin": self.op_owner_begin,
            "owner.confirm": self.op_owner_confirm,
            "owner.status": self.op_owner_status,
            "owner.lock": self.op_owner_lock,
            "owner.logout": self.op_owner_lock,
            "secrets.set": self.op_secrets_set,
            "auth.change_password": self.op_password_change,
            "auth.factors.binding": self.op_factors_binding,
            "auth.factors.prepare": self.op_factors_prepare,
            "auth.factors.commit": self.op_factors_commit,
            "auth.factors.cancel": self.op_factors_cancel,
            "settings.status": self.op_settings_status,
            "policy.get": self.op_policy_get,
            "policy.set": self.op_policy_set,
            "runs.followup": self.op_runs_followup,
            "jobs.attachments": self.op_jobs_attachments,
            "jobs.list": self.op_jobs_list,
            "jobs.start": self.op_jobs_start,
            "jobs.audit": self.op_jobs_audit,
            "jobs.cancel": self.op_jobs_cancel,
            "jobs.proposal": self.op_jobs_proposal,
            "jobs.decide": self.op_jobs_decide,
            **{"work."+op: (lambda args, op=op: self.op_work(op,args)) for op in ("catalog","preparation","status","workspace.create","publish","start")},
            "jobs.artifact": self.op_jobs_artifact,
            **{"workflows."+op: (lambda args, op=op: self.op_workflows(op,args)) for op in WORKFLOW_FIELDS},
            "workflows.watch": self.op_workflow_watch,
        }

    # -- plumbing -----------------------------------------------------------
    def emit(self, name, data):
        try:
            self.sink.event(name, data)
        except Exception:  # noqa: BLE001 - a dead stdout must not kill a job loudly
            pass

    def forget(self, job):
        with self._jobs_lock:
            jobs = self._jobs.get(job.conversation_id) or []
            if job in jobs:
                jobs.remove(job)
            if not jobs:
                self._jobs.pop(job.conversation_id, None)

    def _active_job(self, conversation_id):
        with self._jobs_lock:
            for job in reversed(self._jobs.get(conversation_id) or []):
                if not job.finished.is_set():
                    return job
        return None

    def _register(self, job):
        with self._jobs_lock:
            self._jobs.setdefault(job.conversation_id, []).append(job)

    def shutdown(self):
        with self._workflow_watch_lock:
            for stop in self._workflow_watches.values(): stop.set()
            self._workflow_watches.clear()
        self._forget_owner()
        with self._jobs_lock:
            jobs = [job for group in self._jobs.values() for job in group]
        for job in jobs:
            if job.ws is not None:
                job.ws.close()

    def handle(self, frame):
        if not isinstance(frame, dict):
            raise BrokerError("invalid_json", "request line must be a JSON object")
        rid = frame.get("id")
        if isinstance(rid, bool) or not isinstance(rid, int) or rid < 1:
            raise BrokerError("invalid_request_id", "id must be a positive integer")
        extra = sorted(set(frame) - {"id", "op", "args"})
        if extra:
            # Field names are untrusted too, including on credential requests.
            raise BrokerError("invalid_frame", "unsupported frame fields")
        op = frame.get("op")
        if not isinstance(op, str) or not op.strip():
            raise BrokerError("invalid_frame", "op must be a non-empty string")
        args = frame.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise BrokerError("invalid_args", "args must be an object")
        handler = self._handlers.get(op.strip())
        if handler is None:
            raise BrokerError("unknown_op", "unsupported op")
        result = handler(args)
        if isinstance(result, HandlerResult):
            return result
        return HandlerResult(result)

    def _start(self, job):
        def runner():
            if self._gated:
                job.gate.wait(30)
            job.run()

        self._register(job)
        if not self._gated:
            runner()
            return None
        self._spawn(runner)
        return job.gate.set

    # -- REST helpers -------------------------------------------------------
    def _rest(self, method, path, params=None, body=None, timeout=None, headers=None):
        if headers:
            return self.transport.rest(method, path, params=params, body=body, timeout=timeout,
                                       headers=headers)
        return self.transport.rest(method, path, params=params, body=body, timeout=timeout)

    def _component(self, method, path, label, warnings, deadline=None):
        try:
            return self._rest(method, path, timeout=_remaining(deadline))
        except BrokerError as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            warnings.append("%s: %s" % (label, exc.code))
            return None

    def _owned(self, row, what, field="user_id"):
        """Single-resource guard. The gateway's single-row routes are owner-scoped
        (a foreign row is a 404) and do report `user_id`; when the field is absent
        this cannot refute ownership and does not claim it — the list replies carry
        the machine-readable `scope` instead."""
        if not isinstance(row, dict):
            raise BrokerError("invalid_response", "%s payload is not an object" % what)
        if field in row and row[field] is not None and row[field] != self.sub:
            raise BrokerError("ownership_violation", "%s belongs to another user_id" % what)
        return row

    def _conversation_row(self, conversation_id):
        row = self._rest("GET", "/conversations/" + urllib.parse.quote(conversation_id, safe=""))
        return self._owned(row, "conversation")

    # -- discovery ----------------------------------------------------------
    def op_hello(self, args):
        _only(args, ())
        identity = {
            "sub": self.sub,
            "token_type": self.identity.get("token_type"),
            "actor": self.identity.get("actor"),
            "owner": self.identity.get("owner"),
            "gateway": self.identity.get("gateway"),
            "faces": list(self.identity.get("faces") or []),
            "may_touch": list(self.identity.get("may_touch") or []),
            "may_not_touch": list(self.identity.get("may_not_touch") or []),
            "expires_at": self.identity.get("expires_at"),
        }
        gaps = [dict(gap) for gap in GAPS]
        if not self.allow_approvals:
            gaps.append(dict(APPROVAL_GUARD_GAP))
        gaps.append(dict(LOCAL_TOKEN_GAP))
        operations = {}
        try:
            capabilities = self._rest("GET", "/capabilities", params={"operations": "1", "contract_version": "2"})
            advertised = capabilities.get("operations", {}) if isinstance(capabilities, dict) else {}
            operations = {k: v for k, v in advertised.items() if (k in self._handlers) and isinstance(v, dict)}
        except BrokerError as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
        # Capabilities describe the installation, not the caller's subject. A
        # bounded summary-only challenge verifies portal owner eligibility; it
        # grants no authority and requests no factors. Begin creates a durable
        # challenge, so cache both outcomes for this broker's process lifetime.
        identity["owner_controls_eligible"] = False
        begin = operations.get("owner.begin", {})
        if self.identity.get("token_type") == "portal" and 2 in begin.get("contract_versions", []) and begin.get("available") is not False and begin.get("implemented") is not False:
            if self._owner_eligible is None:
                try:
                    proof = self._portal_owner_rest("/auth/owner/begin", {"contract_version": 2, "operation": "grants.list", "resource": {}})
                    self._owner_eligible = proof.get("summary", {}).get("owner") == self.sub
                except BrokerError as exc:
                    if isinstance(exc, GatewayRateLimited):
                        raise
                    self._owner_eligible = False
            identity["owner_controls_eligible"] = self._owner_eligible is True
        return {"identity": identity, "server": dict(self.server), "gaps": gaps, "operations": operations,
                "owner_session": self.op_owner_status({})["owner_session"]}

    def op_ping(self, args):
        _only(args, ())
        return {"ts": _now_iso()}

    @staticmethod
    def _model_row(entry):
        backend, model = str(entry['backend']), entry.get('model')
        label = entry.get('label') or backend
        suffix = ' / ' + model if isinstance(model, str) and model else ''
        provider = entry.get('provider_label') or (label[:-len(suffix)]
                    if suffix and label.endswith(suffix) else label)
        choices = entry.get('choices')
        if not isinstance(choices, list):
            models = entry.get('models', [model] if model else [])
            models = list(dict.fromkeys(m for m in models if isinstance(m, str) and m))
            default = model if model in models else next(iter(models), None)
            choices = [dict(model=m, label=m, default=m == default) for m in models]
        return dict(id=backend, backend=backend, label=entry.get('label'), model=model,
                    available=bool(entry.get('available')),
                    reason=entry.get('reason') or entry.get('unavailable_reason'),
                    provider_label=provider, choices=choices,
                    tool_capable=bool(entry.get('tool_capable')), billing=entry.get('billing'))

    def op_catalog(self, args):
        _only(args, ())
        warnings = []
        # Four sequential component calls share one wall-clock budget so a degraded
        # gateway cannot make a read-only catalog outlive the client's deadline.
        deadline = time.monotonic() + self.catalog_seconds
        faces_raw = self._component("GET", "/faces", "faces", warnings, deadline)
        models_raw = self._component("GET", "/models/available", "models", warnings, deadline)
        backends_raw = self._component("GET", "/backends/catalog", "backends", warnings, deadline)
        caps_raw = self._component("GET", "/capabilities", "capabilities", warnings, deadline)
        execution_raw = self._component("GET", "/agent-services/catalog?contract_version=2", "execution_v2", warnings, deadline)
        execution_caps = (execution_raw.get("capabilities", {}).get("execution_v2")
                          if isinstance(execution_raw, dict) else None)

        if faces_raw is None and models_raw is None and backends_raw is None and caps_raw is None:
            # Only a failure to reach the gateway is a transport fault. Four HTTP
            # answers (a 500 from a gateway whose core is down, a 403 for a token
            # not permitted on a route) prove the link is up: report an empty
            # catalogue with the codes instead of making the whole portal read
            # "server unreachable" and discard a successful hello.
            if (not isinstance(execution_caps, dict) or 2 not in execution_caps.get("contract_versions", [])) and (
                    not warnings or any(item.endswith(": transport_unavailable") for item in warnings)):
                raise BrokerError("transport_unavailable", "gateway catalog endpoints unreachable")
            warnings.append("catalog: gateway answered with errors; no catalogue loaded")

        # W-5: the token's `faces` claim is an allow-list; the gateway and this
        # broker both refuse a turn on any other face (see op_chat_send). The
        # catalog still lists every registered face so a server pin can be
        # explained, but each row now says whether THIS identity may use it, so
        # the picker cannot offer a face the next send would be refused for.
        allowed_faces = [str(item) for item in (self.identity.get("faces") or [])]
        faces = []
        for entry in _as_list(faces_raw):
            if isinstance(entry, dict) and entry.get("id"):
                face_id = str(entry["id"])
                faces.append({"id": face_id,
                              "name": str(entry.get("name") or entry.get("display_name") or face_id),
                              "allowed": (not allowed_faces) or (face_id in allowed_faces)})

        models = []
        for entry in _as_list(models_raw):
            if not isinstance(entry, dict) or not entry.get("backend"):
                continue
            models.append(self._model_row(entry))

        backends = []
        for entry in _as_list(backends_raw):
            if not isinstance(entry, dict) or not entry.get("backend"):
                continue
            backends.append({"id": str(entry["backend"]),
                             "label": entry.get("label"),
                             "model": entry.get("model"),
                             "kind": entry.get("kind"),
                             "available": bool(entry.get("available")),
                             "unavailable_reason": entry.get("unavailable_reason") or entry.get("reason")})

        caps = caps_raw if isinstance(caps_raw, dict) else {}
        actions = caps.get("actions") if isinstance(caps.get("actions"), list) else []
        # Report the action ids, not a count: the client type and the contract both
        # describe a list, and nothing may render a bare integer as one (S-11).
        action_ids = []
        for entry in actions:
            if isinstance(entry, dict) and entry.get("id"):
                action_ids.append(str(entry["id"]))
            elif isinstance(entry, str):
                action_ids.append(entry)
        if isinstance(caps.get("warnings"), list):
            warnings.extend(str(item) for item in caps["warnings"])
        return {
            "faces": faces,
            "models": models,
            "backends": backends,
            "capabilities": {"faces": len(faces), "backends": len(backends),
                             "execution_v2": execution_caps,
                             "legacy_teams": (execution_raw.get("teams", [])
                                              if isinstance(execution_raw, dict) else []),
                             "execution_models": (execution_raw.get("model_faces", [])
                                                  if isinstance(execution_raw, dict) else []),
                             "actions": action_ids, "warnings": warnings,
                             "allowed_face_ids": allowed_faces,
                             "faces_allowed": sum(1 for row in faces if row.get("allowed"))},
        }

    def _scope_list(self, rows):
        """Ownership scope for a list reply (CONTRACT §4, audit S-1).

        Two honest cases, both machine-readable in `scope`:
          * the payload reports no owner at all (the live `/conversations` and
            `/projects` shapes): the gateway's SQL already scoped the query, the
            broker cannot verify per row, so it says so and reports no
            ownership-based exclusion count at all;
          * the payload reports `user_id` for at least one row: every row is
            compared with the bound sub, differing rows are `excluded`, and rows
            with no owner are `unattributed` and never shown.

        Returns (items, reply_fragment).
        """
        if not rows_carry_owner(rows):
            items, malformed = [], 0
            for row in rows:
                if isinstance(row, dict):
                    items.append(row)
                else:
                    malformed += 1
            scope = scope_note(SCOPE_GATEWAY_ACCOUNT, False, excluded=malformed,
                               unattributed=len(items))
            return items, {"scope": scope}
        kept, excluded, unattributed = attribute_rows(rows, self.sub)
        return kept, {"excluded": excluded, "unattributed": unattributed,
                      "scope": scope_note(SCOPE_ROW_OWNER_FILTER, True, excluded, unattributed)}

    # -- conversations ------------------------------------------------------
    def op_conversations_list(self, args):
        _only(args, ("limit", "offset"))
        limit = _limit_arg(args, "limit", default=20, maximum=LIMIT_CONVERSATIONS)
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=10 ** 6)
        payload = self._rest("GET", "/conversations", {"limit": limit, "offset": offset})
        rows = _as_list(payload.get("items") if isinstance(payload, dict) else None)
        items, scope = self._scope_list(rows)
        reply = {"items": items, "limit": limit, "offset": offset}
        reply.update(scope)
        return reply

    def op_conversations_create(self, args):
        _only(args, ("title", "face_id", "model_preference", "project_id"))
        title = _str_arg(args, "title", required=False, max_bytes=300) or "New Conversation"
        body = {"title": title}
        face = _id_arg(args, "face_id", required=False)
        model = _str_arg(args, "model_preference", required=False, max_bytes=200)
        project = _id_arg(args, "project_id", required=False)
        # Same guard as chat.send: a conversation pinned to a face outside this
        # token's allow-list can never answer (the gateway refuses every turn with
        # face_forbidden), so refuse to create that dead conversation at all.
        allowed_faces = self.identity.get("faces") or []
        if face and allowed_faces and face not in allowed_faces:
            raise BrokerError("face_forbidden",
                              "face is not in this token's allow-list; pick an allowed face "
                              "(catalog lists them with allowed:true)")
        if face:
            body["face_id"] = face
        if model:
            body["model_preference"] = model
        if project:
            body["project_id"] = project
        row = self._rest("POST", "/conversations", body=body)
        return {"conversation": self._owned(row, "conversation")}

    def op_conversations_get(self, args):
        _only(args, ("id",))
        conversation_id = _id_arg(args, "id")
        return {"conversation": self._conversation_row(conversation_id)}

    def op_conversations_rename(self, args):
        _only(args, ("id", "title"))
        conversation_id = _id_arg(args, "id")
        title = _str_arg(args, "title", required=True, max_bytes=300)
        row = self._rest("POST", "/conversations/%s/rename" % urllib.parse.quote(conversation_id, safe=""),
                         body={"title": title})
        return {"conversation": self._owned(row, "conversation")}

    def op_conversations_assign_project(self, args):
        _only(args, ("id", "project_id"))
        conversation_id = _id_arg(args, "id")
        if "project_id" not in args:
            raise BrokerError("invalid_args", "project_id is required (null to unassign)")
        project_id = args["project_id"]
        if project_id is not None:
            if not isinstance(project_id, str) or not _ID_RE.match(project_id):
                raise BrokerError("invalid_args", "project_id must be an id or null")
        row = self._rest("POST", "/conversations/%s/project" % urllib.parse.quote(conversation_id, safe=""),
                         body={"project_id": project_id})
        return {"conversation": self._owned(row, "conversation")}

    def op_messages_page(self, args):
        _only(args, ("id", "limit", "before"))
        conversation_id = _id_arg(args, "id")
        limit = _limit_arg(args, "limit", default=50, maximum=LIMIT_MESSAGES)
        before = _id_arg(args, "before", required=False)
        # S-2: verify conversation ownership before returning the largest row payload
        # in the protocol. The gateway's GET is owner-scoped (a foreign conversation is
        # a 404), so this is the broker's own check rather than a borrowed one.
        try:
            self._conversation_row(conversation_id)
        except BrokerError as exc:
            if exc.code == "gateway_http_404":
                raise BrokerError("conversation_not_found",
                                  "conversation does not exist for the bound user") from None
            if exc.code in ("gateway_http_403", "gateway_http_401"):
                raise BrokerError("conversation_forbidden",
                                  "conversation is not readable by the bound user") from None
            raise
        params = {"limit": limit, "before": before}
        payload = self._rest("GET", "/conversations/%s/messages" % urllib.parse.quote(conversation_id, safe=""),
                             params)
        rows = _as_list(payload.get("items") if isinstance(payload, dict) else None)
        items, used, omitted = [], 0, 0
        content_clamped, metadata_trimmed = 0, 0
        for row in rows:
            if not isinstance(row, dict):
                omitted += 1
                continue
            if row.get("conversation_id") not in (None, conversation_id):
                raise BrokerError("ownership_violation", "message belongs to another conversation")
            item = {key: row.get(key) for key in MESSAGE_FIELDS}
            content = item.get("content")
            content_bytes = len(content.encode("utf-8")) if isinstance(content, str) else 0
            metadata, metadata_bytes = metadata_within(item.get("metadata"))
            if metadata is not item.get("metadata"):
                item["metadata"] = metadata
                metadata_trimmed += 1
            size = content_bytes + metadata_bytes
            remaining = MAX_PAGE_TEXT_BYTES - used
            if size > remaining:
                if items:
                    # Later rows are dropped whole; the page stays readable and says so.
                    omitted += 1
                    continue
                # The first row is clamped rather than dropped, otherwise one oversized
                # message would make the whole history unreadable. Truncation is marked.
                if content_bytes > remaining:
                    item["content"], truncated = clamp_text(content, remaining)
                    if truncated:
                        item["content_bytes"] = content_bytes
                        item["content_truncated"] = True
                        content_clamped += 1
                if metadata_bytes > remaining:
                    item["metadata"] = {"truncated": True, "bytes": metadata_bytes,
                                        "reason": "page budget exhausted"}
                    metadata_trimmed += 1
                    metadata_bytes = 0
                used = MAX_PAGE_TEXT_BYTES
            else:
                used += size
            items.append(item)
        has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
        result = {"items": items, "has_more": has_more or bool(omitted),
                  "scope": {"mechanism": "conversation_scope",
                            "conversation_owner_verified": True,
                            "row_owner_attributable": False}}
        if omitted:
            result["omitted_by_size"] = omitted
        if content_clamped:
            result["content_truncated"] = content_clamped
        if metadata_trimmed:
            result["metadata_truncated"] = metadata_trimmed
        return result

    # -- chat ---------------------------------------------------------------
    def conversation_pins(self, conversation_id):
        row = self._conversation_row(conversation_id)
        return {"face_id": row.get("face_id"),
                "backend": row.get("backend_preference"),
                "model": row.get("model_preference"),
                "profile": row.get("profile"),
                "locale": row.get("locale")}

    def op_chat_send(self, args):
        _only(args, ("conversation_id", "content", "face_id", "model", "backend",
                     "profile", "locale", "research"))
        conversation_id = _id_arg(args, "conversation_id")
        content = _text_arg(args, "content", max_bytes=MAX_CHAT_CONTENT_BYTES)
        face = _id_arg(args, "face_id", required=False)
        model = _str_arg(args, "model", required=False, max_bytes=200)
        backend = _str_arg(args, "backend", required=False, max_bytes=100)
        profile = _str_arg(args, "profile", required=False, max_bytes=64)
        locale = _str_arg(args, "locale", required=False, max_bytes=16)
        research = _bool_arg(args, "research")
        if model and not backend:
            raise BrokerError("invalid_pin",
                              "model requires backend: pins are applied with switch_model")
        if backend and not _ID_RE.match(backend):
            raise BrokerError("invalid_args", "backend is not a valid id")
        if profile and not _PROFILE_RE.match(profile):
            raise BrokerError("invalid_args", "profile has an invalid shape")
        if locale and not _LOCALE_RE.match(locale):
            raise BrokerError("invalid_args", "locale has an invalid shape")
        allowed_faces = self.identity.get("faces") or []
        if face and allowed_faces and face not in allowed_faces:
            raise BrokerError("face_forbidden", "face is not in this token's allow-list")

        if backend and model:
            entries = _as_list(self._rest('GET', '/models/available'))
            entry = next((e for e in entries if isinstance(e, dict) and e.get('backend') == backend), None)
            if entry is None:
                raise BrokerError('invalid_pin', f'{backend} is not configured in this lane.')
            row = self._model_row(entry)
            if model not in {c['model'] for c in row['choices']}:
                raise BrokerError('invalid_pin', f'The model "{model}" is not offered by {row["provider_label"]}.')
            if not row['available']:
                raise BrokerError('invalid_pin', f'{row["provider_label"]} is unavailable: {row["reason"] or "provider unavailable"}')
        request_id = "req-" + uuid.uuid4().hex
        pins = {"face_id": face, "backend": backend, "model": model,
                "profile": profile, "locale": locale}
        job = ChatJob(self, request_id, conversation_id, content, pins, research)
        release = self._start(job)
        return HandlerResult({"request_id": request_id, "accepted": True}, release)

    def op_chat_stop(self, args):
        _only(args, ("conversation_id",))
        conversation_id = _id_arg(args, "conversation_id")
        job = self._active_job(conversation_id)
        stopped = job.request_stop() if job is not None else False
        # Stop also ends task authority after a turn finished or a broker was
        # replaced. The server checks ownership; no mutation is replayed here.
        # Signal the stream first, even if scope revocation loses its reply.
        self._rest("POST","/api/builder/task.end",body={"contract_version":2,"conversation_id":conversation_id})
        if job is None:
            return {"stopped": False, "code": "no_active_stream_in_broker"}
        if not stopped:
            return {"stopped": False, "code": "stream_gone"}
        return {"stopped": True, "code": "stop_requested"}

    def op_chat_answer(self, args):
        """Answer a paused mid-turn question (S2)."""
        _only(args, ("question_id", "answer"))
        question_id = _id_arg(args, "question_id")
        answer = _text_arg(args, "answer", max_bytes=MAX_CHAT_CONTENT_BYTES)
        response = self._rest(
            "POST",
            "/chat/questions/%s/answer" % urllib.parse.quote(question_id, safe=""),
            body={"answer": answer},
        )
        resolved = bool(isinstance(response, dict) and response.get("resolved"))
        return {"resolved": resolved, "question_id": question_id,
                **({'delivered_to_turn':response['delivered_to_turn']} if isinstance(response,dict) and type(response.get('delivered_to_turn')) is bool else {})}

    def op_chat_pins(self, args):
        _only(args, ("conversation_id",))
        conversation_id = _id_arg(args, "conversation_id")
        return self.conversation_pins(conversation_id)

    # -- approvals ----------------------------------------------------------
    def op_approvals_list(self, args):
        _only(args, ("status", "limit", "offset"))
        status = _str_arg(args, "status", required=False, max_bytes=16) or "pending"
        if status not in APPROVAL_STATUSES:
            raise BrokerError("invalid_args", "status must be one of " + ", ".join(sorted(APPROVAL_STATUSES)))
        limit = _limit_arg(args, "limit", default=20, maximum=LIMIT_APPROVALS)
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=10 ** 6)
        payload = self._rest("GET", "/approvals", {"status": status, "limit": limit, "offset": offset})
        rows = _as_list(payload.get("items") if isinstance(payload, dict) else None)
        # Approval rows do carry user_id, so this is a real per-row filter (S-1a).
        items, excluded, unattributed = attribute_rows(rows, self.sub)
        return {"items": items, "excluded": excluded, "unattributed": unattributed,
                "limit": limit, "offset": offset, "status": status,
                "scope": scope_note(SCOPE_ROW_OWNER_FILTER, True, excluded, unattributed)}

    def _approval_row(self, approval_id):
        try:
            row = self._rest("GET", "/approvals/" + urllib.parse.quote(approval_id, safe=""))
        except BrokerError as exc:
            if exc.code == "gateway_http_404":
                raise BrokerError("approval_not_found", "approval does not exist") from None
            raise
        if not isinstance(row, dict):
            raise BrokerError("approval_not_found", "approval is unavailable")
        if row.get("user_id") not in (None, self.sub):
            raise BrokerError("approval_foreign", "approval is owned by another user_id")
        return row

    def op_approvals_get(self, args):
        _only(args, ("approval_id",))
        approval_id = _id_arg(args, "approval_id")
        return {"approval": self._approval_row(approval_id)}

    def op_approvals_decide(self, args):
        _only(args, ("approval_id", "decision", "edit_content"))
        approval_id = _id_arg(args, "approval_id")
        decision = _str_arg(args, "decision", required=True, max_bytes=16)
        if decision not in ("approve", "reject"):
            raise BrokerError("invalid_args", "decision must be approve or reject")
        edit = _text_arg(args, "edit_content", required=False, max_bytes=MAX_CHAT_CONTENT_BYTES)

        # Read-only pre-checks give the UI a precise reason; nothing is written here.
        row = self._approval_row(approval_id)
        if str(row.get("status") or "") != "pending":
            raise BrokerError("approval_stale", "approval is no longer pending")
        if not self.allow_approvals:
            raise BrokerError("approval_not_permitted",
                              "agent-token approval decisions stay refused until the staged gateway guard is active")

        body = {"decision": decision}
        if edit:
            body["edit_content"] = edit
        response = self._rest("POST", "/approvals/%s/decide" % urllib.parse.quote(approval_id, safe=""),
                              body=body)
        if not isinstance(response, dict) or response.get("decision") != "recorded":
            raise BrokerError("invalid_response", "gateway did not record the decision")
        try:
            fresh = self._rest("GET", "/approvals/" + urllib.parse.quote(approval_id, safe=""))
        except Exception as exc:  # noqa: BLE001 - a readback fault is not a decision fault
            # The decision IS recorded. A failed readback must not be dressed up as
            # a failed approval: say what is true and let the UI re-read (R7-R3).
            return {"decision": "recorded",
                    "agent_resume": response.get("agent_resume") or "unknown",
                    "approval": row,
                    "readback_failed": True,
                    "readback_error": getattr(exc, "code", type(exc).__name__),
                    **({"readback_refusal": _error_frame(0, exc)["error"]}
                       if isinstance(exc, GatewayRateLimited) else {})}
        if not isinstance(fresh, dict):
            return {"decision": "recorded",
                    "agent_resume": response.get("agent_resume") or "unknown",
                    "approval": row,
                    "readback_failed": True,
                    "readback_error": "invalid_response"}
        return {"decision": "recorded",
                "agent_resume": response.get("agent_resume") or "unknown",
                "approval": fresh}

    # -- projects -----------------------------------------------------------
    def op_projects_list(self, args):
        _only(args, ("limit", "offset"))
        limit = _limit_arg(args, "limit", default=20, maximum=LIMIT_CONVERSATIONS)
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=10 ** 6)
        payload = self._rest("GET", "/projects", {"limit": limit, "offset": offset})
        rows = _as_list(payload.get("items") if isinstance(payload, dict) else None)
        kept, scope = self._scope_list(rows)
        # S-5: the per-project backfill is up to 25 sequential GETs. Cap the whole
        # fanout at one wall-clock budget and return partial rows with a warning,
        # instead of letting the client's link deadline kill the session.
        deadline = time.monotonic() + self.fanout_seconds
        warnings, items, budget_warned = [], [], False
        for row in kept:
            project = {key: row.get(key) for key in PROJECT_FIELDS}
            if project.get("instructions") is None and len(items) < MAX_PROJECT_DETAIL_FETCH:
                if time.monotonic() >= deadline:
                    if not budget_warned:
                        warnings.append("instructions: fanout_budget_exhausted")
                        budget_warned = True
                else:
                    project["instructions"] = self._project_instructions(
                        row.get("id"), warnings, deadline)
            items.append(project)
        reply = {"items": items, "limit": limit, "offset": offset}
        reply.update(scope)
        if warnings:
            reply["warnings"] = warnings
        return reply

    def _project_instructions(self, project_id, warnings, deadline=None):
        """List rows omit `instructions`; fill it from the single-resource route."""
        if not isinstance(project_id, str) or not _ID_RE.match(project_id):
            return None
        try:
            row = self._rest("GET", "/projects/" + urllib.parse.quote(project_id, safe=""),
                             timeout=_remaining(deadline))
        except BrokerError as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            warnings.append("instructions(%s): %s" % (project_id, exc.code))
            return None
        if not isinstance(row, dict):
            return None
        if "user_id" in row and row["user_id"] is not None and row["user_id"] != self.sub:
            return None
        return row.get("instructions")

    def op_projects_create(self, args):
        _only(args, ("name", "description", "instructions"))
        name = _str_arg(args, "name", required=True, max_bytes=200)
        body = {"name": name}
        description = _str_arg(args, "description", required=False, max_bytes=4000, empty_ok=True)
        instructions = _str_arg(args, "instructions", required=False, max_bytes=16000, empty_ok=True)
        if description is not None:
            body["description"] = description
        if instructions is not None:
            body["instructions"] = instructions
        row = self._rest("POST", "/projects", body=body)
        return {"project": self._owned(row, "project")}

    def op_projects_update(self, args):
        _only(args, ("id", "name", "description", "instructions"))
        project_id = _id_arg(args, "id")
        body = {}
        name = _str_arg(args, "name", required=False, max_bytes=200)
        description = _str_arg(args, "description", required=False, max_bytes=4000, empty_ok=True)
        instructions = _str_arg(args, "instructions", required=False, max_bytes=16000, empty_ok=True)
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if instructions is not None:
            body["instructions"] = instructions
        if not body:
            raise BrokerError("invalid_args", "no updatable field supplied")
        row = self._rest("POST", "/projects/" + urllib.parse.quote(project_id, safe=""), body=body)
        return {"project": self._owned(row, "project")}

    def op_projects_delete(self, args):
        _only(args, ("id",))
        project_id = _id_arg(args, "id")
        try:
            row = self._rest("DELETE", "/projects/" + urllib.parse.quote(project_id, safe=""))
        except BrokerError as exc:
            if exc.code == "gateway_http_404":
                raise BrokerError("project_not_found",
                                  "project does not exist for the bound user") from None
            raise
        # The gateway's DELETE is owner-scoped, but derive success from the response
        # rather than asserting it (S-3): a reported owner must be ours and, when the
        # gateway reports a state, that state must be archived/deleted.
        if isinstance(row, dict):
            self._owned(row, "project")
            state = row.get("status")
            if state is not None and str(state) not in ("archived", "deleted"):
                raise BrokerError("invalid_response", "gateway did not confirm deletion")
        return {"deleted": True}

    # Shared S2B owner-read contract; keep identical in both broker copies.
    def _owner_read(self, method, route, body=None):
        try:
            value = self._rest(method, route, body=body)
        except BrokerError as exc:
            if exc.code == "gateway_http_404" and exc.detail not in ("not_found", "source_not_found"):
                raise BrokerError("unavailable", "not available on this server") from None
            if exc.detail in ("snapshot_expired", "invalid_cursor", "not_found", "scope_denied",
                              "file_too_large", "directory_too_large", "revision_conflict",
                              "snapshot_capacity", "snapshot_too_large", "lane_unavailable",
                              "projects_unavailable", "memory_unavailable", "invalid_path", "invalid_args"):
                raise BrokerError(exc.detail, exc.detail) from None
            raise
        if not isinstance(value, dict):
            raise BrokerError("invalid_response", "owner read payload is not an object")
        return value

    def op_memory_list(self, args):
        _only(args, ("limit", "cursor", "kind"))
        body = {"limit": _int_arg(args, "limit", default=50, minimum=1, maximum=50)}
        for key in ("cursor", "kind"):
            value = _str_arg(args, key, max_bytes=200)
            if value is not None:
                body[key] = value
        if body.get("kind") not in (None, "family_memory", "bob_event", "bob_fact"):
            raise BrokerError("invalid_args", "invalid kind")
        value = self._owner_read("POST", "/agent-memory/list", body)
        if not isinstance(value.get("items"), list) or not isinstance(value.get("snapshot"), str) or not isinstance(value.get("coverage"), dict) or "next_cursor" not in value:
            raise BrokerError("invalid_response", "invalid memory page")
        return value

    def op_files_roots(self, args):
        _only(args, ())
        value = self._owner_read("GET", "/files/roots")
        if not isinstance(value.get("items"), list):
            raise BrokerError("invalid_response", "invalid roots")
        return value

    def _file_args(self, args, read=False):
        keys = ("attachment_id", "path", "offset", "max_bytes") if read else ("attachment_id", "path", "cursor")
        _only(args, keys)
        aid = _str_arg(args, "attachment_id", required=True, max_bytes=100)
        path = _str_arg(args, "path", required=read, empty_ok=True, max_bytes=2048) or ""
        if path:
            parts = path.split("/")
            if len(parts) > 32 or any(p in ("", ".", "..") for p in parts) or any(c in path for c in ("\\", ":", "%")) or any(ord(c) < 32 for c in path):
                raise BrokerError("invalid_args", "invalid relative path")
        elif read:
            raise BrokerError("invalid_args", "path required")
        body = {"attachment_id": aid, "path": path}
        if read:
            body["offset"] = _int_arg(args, "offset", default=0, minimum=0, maximum=16777216)
            body["max_bytes"] = _int_arg(args, "max_bytes", default=65536, minimum=1, maximum=65536)
        else:
            cursor = _str_arg(args, "cursor", max_bytes=200)
            if cursor is not None:
                body["cursor"] = cursor
        return body

    def op_files_list(self, args):
        value = self._owner_read("POST", "/files/list", self._file_args(args))
        if not isinstance(value.get("items"), list) or "next_cursor" not in value:
            raise BrokerError("invalid_response", "invalid directory page")
        return value

    def op_files_read(self, args):
        value = self._owner_read("POST", "/files/read", self._file_args(args, read=True))
        if not isinstance(value.get("content"), str) or value.get("encoding") not in ("utf-8", "base64") or not isinstance(value.get("revision"), str) or type(value.get("truncated")) is not bool or "next_offset" not in value:
            raise BrokerError("invalid_response", "invalid file chunk")
        return value

    # -- memory -------------------------------------------------------------
    def op_memory_status(self, args):
        _only(args, ())
        payload = self._rest("GET", "/agent-memory/status")
        result = dict(payload) if isinstance(payload, dict) else {"payload": payload}
        result["coverage_note"] = _coverage_note(result)
        return result

    def op_memory_search(self, args):
        _only(args, ("query", "limit", "offset"))
        query = _str_arg(args, "query", required=True, max_bytes=MAX_SEARCH_QUERY_BYTES)
        limit = _limit_arg(args, "limit", default=10, maximum=LIMIT_MEMORY_SEARCH)
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=100000)
        payload = self._rest("POST", "/agent-memory/search",
                             body={"query": query, "limit": limit, "offset": offset})
        if not isinstance(payload, dict):
            raise BrokerError("invalid_response", "memory search payload is not an object")
        items = [{"source_id": row.get("source_id"), "owner": row.get("owner"),
                  "kind": row.get("kind"), "provenance": row.get("provenance"),
                  "excerpt": row.get("excerpt"), "truncated": bool(row.get("truncated"))}
                 for row in _as_list(payload.get("items")) if isinstance(row, dict)]
        coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
        return {"items": items, "coverage": coverage}

    def op_memory_read(self, args):
        _only(args, ("source_id", "offset", "max_chars"))
        source_id = _str_arg(args, "source_id", required=True, max_bytes=200)
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=10000000)
        max_chars = _limit_arg(args, "max_chars", default=6000, maximum=MAX_MEMORY_CHARS)
        payload = self._rest("POST", "/agent-memory/read",
                             body={"source_id": source_id, "offset": offset, "max_chars": max_chars})
        if not isinstance(payload, dict):
            raise BrokerError("invalid_response", "memory read payload is not an object")
        return payload

    def op_memory_save(self, args):
        _only(args, ("content", "category"))
        content = _text_arg(args, "content", max_bytes=MAX_MEMORY_CHARS * 4)
        if len(content) > MAX_MEMORY_CHARS:
            raise BrokerError("invalid_args", "content exceeds %d characters" % MAX_MEMORY_CHARS)
        category = _str_arg(args, "category", required=True, max_bytes=16)
        if category not in MEMORY_CATEGORIES:
            raise BrokerError("invalid_args", "category must be one of " + ", ".join(sorted(MEMORY_CATEGORIES)))
        payload = self._rest("POST", "/agent-memory/save",
                             body={"content": content, "category": category})
        if not isinstance(payload, dict):
            raise BrokerError("invalid_response", "memory save payload is not an object")
        return payload

    # -- runs ---------------------------------------------------------------
    def _require_execution_v2(self):
        # Control/readback must work even while the model catalog is unavailable.
        catalog = self._rest("GET", "/capabilities", params={"operations": "1", "contract_version": "2"})
        operations = catalog.get("operations", {}) if isinstance(catalog, dict) else {}
        capabilities = operations.get("runs.start", {}) if isinstance(operations, dict) else {}
        if not isinstance(capabilities, dict) or 2 not in capabilities.get("contract_versions", []):
            raise BrokerError("client_upgrade_required", "Server does not advertise execution v2; no reduced retry was sent")
        return capabilities

    def op_teams_list(self, args):
        _only(args, ("cursor", "limit", "contract_version"))
        if type(args.get("contract_version", 2)) is not int or args.get("contract_version", 2) != 2:
            raise BrokerError("unsupported_run_contract", "teams.list requires v2")
        self._require_execution_v2()
        return self._rest("GET", "/teams", params={**args, "contract_version": 2})

    def op_teams_save(self, args):
        _only(args, ("definition", "expected_revision", "request_key", "contract_version"))
        if type(args.get("contract_version", 2)) is not int or args.get("contract_version", 2) != 2:
            raise BrokerError("unsupported_run_contract", "teams.save requires v2")
        _str_arg(args, "request_key", required=True, max_bytes=128)
        if not isinstance(args.get("definition"), dict) or "expected_revision" not in args:
            raise BrokerError("invalid_args", "definition and expected_revision are required")
        self._require_execution_v2()
        return self._rest("POST", "/teams", body={**args, "contract_version": 2})

    def _ledger_remember(self, row, team=None, worker=None):
        """Record/refresh one run row. Never raises: a ledger fault is a warning on
        the op that already succeeded, because a dispatch must not be failed by
        bookkeeping."""
        if not isinstance(row, dict):
            return "run_ledger_invalid_response"
        run_id = row.get("id")
        if not isinstance(run_id, str) or not run_id:
            return "run_ledger_no_run_id"
        try:
            try:
                rows = self.ledger.rows()
            except BrokerError as exc:
                # A corrupt ledger must not block recording; record() moves the
                # broken bytes aside and starts a fresh file.
                if exc.code != "run_ledger_corrupt":
                    raise
                rows = []
            previous = None
            for existing in rows:
                if existing.get("run_id") == run_id:
                    previous = existing
                    break
            self.ledger.record(_ledger_row(row, team=team, worker=worker, previous=previous))
            return None
        except BrokerError as exc:
            return exc.code
        except OSError:
            # ENOSPC/EIO/ENOLCK from the filesystem or flock: the run op already
            # succeeded, so this is a warning, never a failed dispatch.
            return "run_ledger_io"

    def _with_ledger_warning(self, reply, warning):
        if warning:
            reply["warnings"] = ["run_ledger: " + warning]
        return reply

    def op_runs_start(self, args):
        if "contract_version" in args:
            _only(args, ("contract_version", "team", "team_revision", "task", "request_key", "budget",
                         "project_id", "conversation_id", "sprint_id", "occurrence_id", "evidence_ids", "acceptance", "intent_id", "job_coverage"))
            if type(args["contract_version"]) is not int or args["contract_version"] != 2:
                raise BrokerError("unsupported_run_contract", "runs.start requires v2")
            _str_arg(args, "request_key", required=True, max_bytes=128)
            _text_arg(args, "task", max_bytes=MAX_TASK_BYTES)
            _str_arg(args, "team", required=True, max_bytes=64)
            if _int_arg(args, "team_revision", minimum=1, maximum=9223372036854775807) is None:
                raise BrokerError("invalid_args", "team_revision is required")
            budget = args.get("budget")
            if not isinstance(budget, dict):
                raise BrokerError("invalid_args", "budget is required")
            _only(budget, ("start_calls", "wall_mode", "wall_seconds", "max_job_attempts", "job_seconds_budget"))
            for field in ("max_job_attempts", "job_seconds_budget"):
                if field in budget and not (field == "job_seconds_budget" and budget[field] is None):
                    if _int_arg(budget, field, minimum=1, maximum=9223372036854775807) is None:
                        raise BrokerError("invalid_args", field + " is required")
            if _int_arg(budget, "start_calls", minimum=1, maximum=9223372036854775807) is None:
                raise BrokerError("invalid_args", "start_calls is required")
            if budget.get("wall_mode") not in ("none", "fixed", "follow_budget"):
                raise BrokerError("invalid_args", "invalid wall_mode")
            if budget["wall_mode"] == "none":
                if budget.get("wall_seconds") is not None:
                    raise BrokerError("invalid_args", "none wall mode requires null seconds")
            else:
                if _int_arg(budget, "wall_seconds", minimum=1, maximum=9223372036854775807) is None:
                    raise BrokerError("invalid_args", "wall_seconds is required")
            self._require_execution_v2()
            # Preserve the caller's key exactly; never generate or retry a new key.
            return self._rest("POST", "/agent-services/runs", body=args, timeout=self.fanout_seconds)
        _only(args, ("team", "task", "max_model_calls", "max_seconds", "worker"))
        team = _str_arg(args, "team", required=True, max_bytes=64)
        if team not in RUN_TEAMS:
            raise BrokerError("invalid_args", "team must be one of " + ", ".join(sorted(RUN_TEAMS)))
        task = _text_arg(args, "task", max_bytes=MAX_TASK_BYTES)
        # CONTRACT §3 allows 1..4; the budget is validated here, before any dispatch.
        # The discovered server build runs the team at 4 seats, so a smaller budget is
        # forwarded as asked and a gateway refusal surfaces as its own typed error.
        calls = _int_arg(args, "max_model_calls", default=4, minimum=1, maximum=4)
        seconds = _int_arg(args, "max_seconds", default=900, minimum=30, maximum=900)
        worker = _str_arg(args, "worker", required=False, max_bytes=64)
        if worker not in RUN_WORKERS:
            raise BrokerError("invalid_args", "worker is not supported by this server")

        body = {"team": team, "task": task, "max_model_calls": calls,
                "max_seconds": seconds, "require_bulk": False}
        if worker:
            body["worker"] = worker
        # S-5: a run dispatch is bounded like any other fanout so a slow gateway
        # answers with a typed error instead of the client killing the link. There is
        # no retry and no run list: an unknown start outcome must be inspected, not resent.
        deadline = time.monotonic() + self.fanout_seconds
        row = self._rest("POST", "/agent-services/runs", body=body,
                         timeout=_remaining(deadline))
        return self._with_ledger_warning({"run": row},
                                         self._ledger_remember(row, team=team, worker=worker))

    def op_runs_list(self, args):
        if "contract_version" in args:
            _only(args, ("contract_version", "cursor", "limit", "project_id", "status"))
            if type(args["contract_version"]) is not int or args["contract_version"] != 2:
                raise BrokerError("unsupported_run_contract", "runs.list requires v2")
            self._require_execution_v2()
            return self._rest("GET", "/agent-services/runs", params=args, timeout=self.fanout_seconds)
        _only(args, ("limit", "offset"))
        limit = _limit_arg(args, "limit", default=20, maximum=50)
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=10 ** 6)
        rows = self.ledger.rows()
        return {"items": rows[offset:offset + limit], "total": len(rows),
                "source": "legacy_portal_ledger",
                "limit": limit, "offset": offset,
                "scope": scope_note(SCOPE_PORTAL_RUN_LEDGER, True, 0, 0),
                "caveats": list(RUN_LEDGER_CAVEATS)}

    def op_runs_get(self, args):
        if "contract_version" in args:
            _only(args, ("contract_version", "run_id", "after_event_seq", "receipt_cursor"))
            if type(args["contract_version"]) is not int or args["contract_version"] != 2:
                raise BrokerError("unsupported_run_contract", "runs.get requires v2")
            run_id = _id_arg(args, "run_id")
            self._require_execution_v2()
            return self._rest("GET", "/agent-services/runs/" + urllib.parse.quote(run_id, safe=""),
                              params={k: v for k, v in args.items() if k != "run_id"}, timeout=self.fanout_seconds)
        _only(args, ("run_id",))
        run_id = _id_arg(args, "run_id")
        deadline = time.monotonic() + self.fanout_seconds
        row = self._rest("GET", "/agent-services/runs/" + urllib.parse.quote(run_id, safe=""),
                         timeout=_remaining(deadline))
        return self._with_ledger_warning({"run": row}, self._ledger_remember(row))

    def op_runs_cancel(self, args):
        if "contract_version" in args:
            _only(args, ("contract_version", "run_id", "request_key"))
            if type(args["contract_version"]) is not int or args["contract_version"] != 2:
                raise BrokerError("unsupported_run_contract", "runs.cancel requires v2")
            run_id = _id_arg(args, "run_id")
            _str_arg(args, "request_key", required=True, max_bytes=128)
            self._require_execution_v2()
            return self._rest("POST", "/agent-services/runs/" + urllib.parse.quote(run_id, safe="") + "/cancel",
                              body={"contract_version": 2, "request_key": args["request_key"]}, timeout=self.fanout_seconds)
        _only(args, ("run_id",))
        run_id = _id_arg(args, "run_id")
        deadline = time.monotonic() + self.fanout_seconds
        row = self._rest("POST", "/agent-services/runs/%s/cancel" % urllib.parse.quote(run_id, safe=""),
                         body={}, timeout=_remaining(deadline))
        return self._with_ledger_warning({"run": row}, self._ledger_remember(row))

    def _require_owner_operation(self, operation):
        result = self._rest("GET", "/capabilities", params={"operations": 1, "contract_version": 2}, timeout=self.fanout_seconds)
        advertised = result.get("operations", {}).get(operation, {}) if isinstance(result, dict) else {}
        if 2 not in advertised.get("contract_versions", []):
            raise BrokerError("client_upgrade_required", "Server does not advertise " + operation + "; no retry was sent")
        if advertised.get("available") is False or advertised.get("implemented") is False:
            raise BrokerError("operation_unavailable", str(advertised.get("reason") or operation))
        return advertised

    def op_runs_resume(self, args):
        _only(args, ("contract_version", "run_id", "expected_revision", "request_key", "budget"))
        if type(args.get("contract_version")) is not int or args["contract_version"] != 2:
            raise BrokerError("unsupported_run_contract", "runs.resume requires v2")
        run_id = _id_arg(args, "run_id")
        _str_arg(args, "request_key", required=True, max_bytes=128)
        if _int_arg(args, "expected_revision", minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError("invalid_args", "expected_revision is required")
        self._require_owner_operation("runs.resume")
        return self._owner_rest("POST", "/agent-services/runs/" + urllib.parse.quote(run_id, safe="") + "/resume",
                          body={k: v for k, v in args.items() if k != "run_id"}, timeout=self.fanout_seconds)

    def _require_flags(self, args, operation):
        if type(args.get("contract_version")) is not int or args["contract_version"] != 2:
            raise BrokerError("unsupported_run_contract", "flags require v2")
        self._require_owner_operation(operation)

    def op_flags_list(self, args):
        _only(args, ("contract_version", "run_id", "cursor", "limit", "status"))
        self._require_flags(args, "flags.list")
        return self._rest("GET", "/agent-services/flags", params=args, timeout=self.fanout_seconds)

    def op_flags_get(self, args):
        _only(args, ("contract_version", "flag_id", "after_event_seq", "job_cursor"))
        flag_id = _id_arg(args, "flag_id")
        self._require_flags(args, "flags.get")
        return self._rest("GET", "/agent-services/flags/" + urllib.parse.quote(flag_id, safe=""),
                          params={k: v for k, v in args.items() if k != "flag_id"}, timeout=self.fanout_seconds)

    def op_flags_resolve(self, args):
        _only(args, ("contract_version", "flag_id", "expected_revision", "request_key", "decision", "guidance"))
        flag_id = _id_arg(args, "flag_id")
        _str_arg(args, "request_key", required=True, max_bytes=128)
        _str_arg(args, "guidance", required=True, max_bytes=16000)
        if args.get("decision") not in {"inspected", "resume_with_guidance", "authorize_budget", "cancel"}:
            raise BrokerError("invalid_args", "invalid flag decision")
        if _int_arg(args, "expected_revision", minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError("invalid_args", "expected_revision is required")
        if len(args["guidance"]) > 4000:
            raise BrokerError("invalid_args", "guidance exceeds 4000 characters")
        self._require_flags(args, "flags.resolve")
        return self._owner_rest("POST", "/agent-services/flags/" + urllib.parse.quote(flag_id, safe="") + "/resolve",
                                body={k: v for k, v in args.items() if k != "flag_id"}, timeout=self.fanout_seconds)

    def op_budgets_set(self, args):
        _only(args, ("contract_version", "project_id", "call_limit", "expected_revision", "request_key"))
        if type(args.get("contract_version")) is not int or args["contract_version"] != 2:
            raise BrokerError("unsupported_run_contract", "budgets.set requires v2")
        _str_arg(args, "project_id", required=True, max_bytes=128)
        _str_arg(args, "request_key", required=True, max_bytes=128)
        if _int_arg(args, "call_limit", minimum=1, maximum=9223372036854775807) is None or "expected_revision" not in args:
            raise BrokerError("invalid_args", "call_limit and expected_revision are required")
        if args["expected_revision"] is not None:
            _int_arg(args, "expected_revision", minimum=1, maximum=9223372036854775807)
        self._require_owner_operation("budgets.set")
        return self._owner_rest("POST", "/agent-services/budgets", body=args, timeout=self.fanout_seconds)

    def _forget_owner(self):
        if self._owner_transport is not None:
            _OWNER_SECRETS.discard(getattr(self._owner_transport, "_token", ""))
        self._owner_transport = None
        self._owner_session = {"active": False}

    def _owner_rest(self, method, path, **kwargs):
        if (not self._owner_transport or not self._owner_session.get("active")
                or self._owner_session.get("expires_at", 0) <= time.time()):
            self._forget_owner()
            raise BrokerError("owner_confirmation_required")
        try:
            return self._owner_transport.rest(method, path, **kwargs)
        except BrokerError as exc:
            if exc.code in {"gateway_http_401", "gateway_http_403", "owner_session_invalid"}:
                self._forget_owner()
            raise

    def _portal_owner_rest(self, path, args):
        if self.identity.get("token_type") != "portal":
            raise BrokerError("owner_portal_required")
        if path in {"/api/builder/mode.set", "/api/builder/approve", "/api/builder/network.set"}:
            return self._owner_rest("POST", path, body=args,
                                    timeout=650 if path.endswith("/approve") else self.fanout_seconds)
        return self.transport.rest("POST", path, body=args, timeout=self.fanout_seconds,
                                   owner_headers={"X-Bob-Broker-Link": self._owner_link_nonce})

    def _safe_owner_metadata(self, result):
        value = result.get("owner_session") if isinstance(result, dict) else None
        operations = {"secrets.set", "policy.get", "policy.set", "runs.resume", "budgets.set", "budgets.get", "budgets.list", "budgets.remove", "grants.issue",
                      "grants.list", "grants.get", "grants.revoke", "owner.status", "owner.lock", "owner.logout", "flags.resolve", "jobs.decide", "work.workspace.create", "work.publish", "builder.mode.set", "builder.approve", "builder.network.set", "workflows.grant_start", "workflows.resume", "workflows.gate"}
        if (not isinstance(value, dict) or value.get("owner") != self.sub or value.get("actor") != self.sub
                or value.get("active") is not True or type(value.get("expires_at")) not in {int, float}
                or not time.time() < value["expires_at"] <= time.time() + 301
                or type(value.get("credential_epoch")) is not int or value["credential_epoch"] < 1
                or not isinstance(value.get("allowed_operations"), list)
                or any(not isinstance(op, str) or op not in operations for op in value["allowed_operations"])
                or not isinstance(value.get("lane"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["lane"])):
            raise BrokerError("invalid_owner_session")
        return {key: value[key] for key in ("active", "owner", "actor", "lane", "expires_at", "allowed_operations", "credential_epoch")}

    def op_owner_begin(self, args):
        _only(args, ("operation", "resource", "proposal_revision", "contract_version"))
        self._require_owner_operation("owner.begin")
        return self._portal_owner_rest("/auth/owner/begin", args)

    def op_owner_confirm(self, args):
        _only(args, ("challenge_id", "password", "totp", "confirmation_mode", "contract_version"))
        self._require_owner_operation("owner.confirm")
        _str_arg(args, "challenge_id", required=True, max_bytes=128)
        if "confirmation_mode" in args:
            if args["confirmation_mode"] != "signed_in_user" or "password" in args or "totp" in args:
                raise BrokerError("invalid_args", "invalid owner confirmation mode")
            factors = []
        else:
            factors = [_str_arg(args, key, required=True, max_bytes=1024) for key in ("password", "totp")]
        _OWNER_SECRETS.update(factors)
        try:
            # Exactly one submission; an uncertain reply always needs fresh proof.
            result = self._portal_owner_rest("/auth/owner/confirm", args)
            token = result.get("owner_bearer") if isinstance(result, dict) else None
            if not isinstance(token, str) or not token.startswith("bos1."):
                raise BrokerError("invalid_owner_session")
            _OWNER_SECRETS.add(token)
            try:
                session = self._safe_owner_metadata(result)
                portal_id = decode_claims(self.transport._bearer()).get("jti")
                if not portal_id:
                    raise BrokerError("owner_portal_required")
                self._forget_owner()
                self._owner_transport = LiveTransport(self.transport.base, token, timeout=self.transport.timeout,
                    owner_headers={"X-Bob-Broker-Link": self._owner_link_nonce, "X-Bob-Portal-Credential": portal_id})
                self._owner_session = session
            except Exception:
                _OWNER_SECRETS.discard(token)
                raise
            safe = {"owner_session": dict(session)}
            notice = result.get('security_notice')
            if isinstance(notice, str) and re.fullmatch(r'two-factor was reset on [0-9]{4}-[0-9]{2}-[0-9]{2}', notice):
                safe['security_notice'] = notice
            return safe
        except BrokerError as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            # Don't relay credential-bearing/malformed upstream error text.
            raise BrokerError(exc.code, "Owner confirmation failed; credentials were not retried", exc.retry_after) from None
        finally:
            _OWNER_SECRETS.difference_update(factors)

    def op_owner_status(self, args):
        _only(args, ())
        if self._owner_transport:
            try:
                result = self._owner_rest("GET", "/auth/owner/status", timeout=self.fanout_seconds)
                session = self._safe_owner_metadata(result)
                if session != self._owner_session:
                    raise BrokerError("owner_session_changed")
            except BrokerError as exc:
                if isinstance(exc, GatewayRateLimited):
                    raise
                self._forget_owner()
        return {"owner_session": dict(self._owner_session)}

    def op_owner_lock(self, args):
        _only(args, ())
        try:
            if self._owner_transport:
                self._owner_rest("POST", "/auth/owner/lock", body={}, timeout=self.fanout_seconds)
        finally:
            self._forget_owner()
        return {"owner_session": {"active": False}}

    def op_grants_prepare(self, args):
        _only(args, ("contract_version", "request", "run_id", "expected_revision", "request_key"))
        self._require_owner_operation("grants.prepare")
        _str_arg(args, "request_key", required=True, max_bytes=128)
        return self._rest("POST", "/agent-services/grant-proposals", body={**args, "contract_version": 2}, timeout=self.fanout_seconds)

    def op_workspace_browse(self, args):
        _only(args, ("contract_version", "project_id", "path"))
        if type(args.get("contract_version", 2)) is not int or args.get("contract_version", 2) != 2:
            raise BrokerError("client_upgrade_required")
        value = _str_arg(args, "project_id", required=True, max_bytes=128)
        if value != args["project_id"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
            raise BrokerError("invalid_args", "invalid project identifier")
        path = args.get("path", "")
        if (not isinstance(path, str) or len(path.encode()) > 2048 or
                any(c in path for c in ("\\", ":", "%")) or any(ord(c) < 32 or ord(c) == 127 for c in path) or
                path and any(part in {"", ".", ".."} for part in path.split("/"))):
            raise BrokerError("invalid_args", "relative workspace path required")
        return self._rest("POST", "/api/workspace/browse", body={**args, "contract_version": 2}, timeout=self.fanout_seconds)

    def op_pc(self,operation,args):
        fields=() if operation=="pending" else ("conversation_id",) if operation=="end" else ("payload","signature")
        _only(args,("contract_version",*fields))
        if args.get("contract_version")!=2 or type(args.get("contract_version")) is not int:
            raise BrokerError("client_upgrade_required")
        return self._rest("POST","/api/pc/"+operation,body=args,timeout=self.fanout_seconds)

    # Builder app/server broker contract. Keep identical in both copies.
    def op_builder(self, operation, args):
        fields = {"mode.get": ("conversation_id",),
                  "project.result": ("project_id",),
                  "task.end": ("conversation_id",),
                  "mode.set": ("conversation_id", "mode", "expected_revision", "request_key", "yolo_acknowledged"),
                  "approval.get": ("approval_id",),
                  "approve": ("approval_id", "decision", "request_key"),
                  "preview": ("conversation_id", "path"),
                  "network.set": ("project_id", "add", "remove", "expected_revision", "request_key"),
                  "network_log": ("conversation_id", "job_id")}[operation]
        _only(args, ("contract_version", *fields))
        if type(args.get("contract_version", 2)) is not int or args.get("contract_version", 2) != 2:
            raise BrokerError("client_upgrade_required")
        if operation == "network_log" and (("conversation_id" in args) == ("job_id" in args)):
            raise BrokerError("invalid_args", "choose one conversation or job")
        for field in ("conversation_id", "approval_id", "project_id", "job_id", "request_key"):
            if field in fields and (operation != "network_log" or field in args):
                value = _str_arg(args, field, required=True, max_bytes=128)
                if value != args[field] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
                    raise BrokerError("invalid_args", "invalid builder identifier")
        if operation == "mode.set":
            if not isinstance(args.get("mode"), str) or args["mode"] not in {"suggest", "approve", "auto", "yolo"}:
                raise BrokerError("invalid_args", "invalid builder mode")
            if _int_arg(args, "expected_revision", minimum=0, maximum=9223372036854775807) is None:
                raise BrokerError("invalid_args", "builder revision required")
            if args["mode"] == "yolo":
                if args.get("yolo_acknowledged") is not True:
                    raise BrokerError("yolo_ack_required")
            elif "yolo_acknowledged" in args:
                raise BrokerError("invalid_args", "unexpected yolo acknowledgement")
        if operation == "network.set":
            if _int_arg(args, "expected_revision", minimum=0, maximum=9223372036854775807) is None:
                raise BrokerError("invalid_args", "builder revision required")
            for field in ("add", "remove"):
                if field not in args:
                    continue
                domains = args[field]
                if (not isinstance(domains, list) or len(domains) > 256
                        or any(not isinstance(d, str) or len(d) > 253 or "." not in d
                               or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in d.split("."))
                               or d.rsplit(".", 1)[-1].isdigit() for d in domains)
                        or len(set(domains)) != len(domains)):
                    raise BrokerError("invalid_args", "canonical DNS hostnames required")
            if (not args.get("add") and not args.get("remove")) or set(args.get("add", [])) & set(args.get("remove", [])):
                raise BrokerError("invalid_args", "nonempty disjoint domain changes required")
        if operation == "approve" and (not isinstance(args.get("decision"), str) or args["decision"] not in {"allow_once", "allow_session", "allow_project", "deny"}):
            raise BrokerError("invalid_args", "invalid builder decision")
        if operation == "preview":
            _str_arg(args, "path", required=True, max_bytes=4096)
            path = args["path"]
            if (any(c in path for c in ("\\", ":")) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path)
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise BrokerError("invalid_args", "relative workspace path required")
        self._require_owner_operation("builder." + operation)
        route = "/api/builder/" + operation
        body = {**args, "contract_version": 2}
        if operation in {"mode.set", "approve", "network.set"}:
            return self._portal_owner_rest(route, body)
        return self._rest("POST", route, body=body, timeout=self.fanout_seconds)

    def op_workflows(self, operation, args):
        _only(args, ('contract_version', *WORKFLOW_FIELDS[operation]))
        if type(args.get('contract_version')) is not int or args['contract_version'] != 1:
            raise BrokerError('workflow_contract_required')
        a = dict(args)
        for key in ('id','proposal_id','run_id','wait_id','artifact_id'):
            if key in a:
                value = _id_arg(a,key)
                if value != a[key]: raise BrokerError('invalid_args')
        for key in ('revision','expected_revision','proposal_revision','after_seq','limit','offset','max_bytes'):
            if a.get(key) is not None:
                _int_arg(a,key,minimum=0 if key in {'after_seq','offset'} else 1,maximum=9223372036854775807)
        route_ids = set()
        def take(key):
            route_ids.add(key)
            return urllib.parse.quote(_id_arg(a,key),safe='')
        if operation in {'catalog','list','get','save','copy','delete'}:
            path = '/workflows'
            if operation=='catalog': path += '/catalog'
            elif operation in {'get','copy','delete'}:
                path += '/'+take('id')
                if operation in {'copy','delete'}: path += '/'+operation
        elif operation in {'propose','proposals','proposal','grant_start','propose_again'}:
            path = '/workflow-proposals'
            if operation not in {'propose','proposals'}: path += '/'+take('proposal_id')
            if operation=='grant_start': path += '/start'
            if operation=='propose_again': path += '/propose-again'
        else:
            path = '/workflow-runs'
            if operation!='runs': path += '/'+take('run_id')
            if operation in {'events','pause','resume','stop'}: path += '/'+operation
            if operation=='message': path += '/messages'
            if operation=='gate': path += '/gates/'+take('wait_id')
            if operation=='artifact': path += '/artifacts/'+take('artifact_id')
        read = operation in {'catalog','list','get','proposals','proposal','runs','run','events','artifact'}
        payload = {k:v for k,v in a.items() if k not in route_ids}
        if operation == 'proposals' and 'include_expired' in payload:
            if type(payload['include_expired']) is not bool:
                raise BrokerError('invalid_args')
            payload['include_expired'] = 'true' if payload['include_expired'] else 'false'
        rest = self._owner_rest if operation in {'grant_start','resume','gate'} else self._rest
        if operation == 'events' and 'wait' in a:
            if type(a['wait']) not in (int, float) or not 0 <= a['wait'] <= 25:
                raise BrokerError('invalid_args')
        timeout = max(self.fanout_seconds, 35) if operation == 'events' else self.fanout_seconds
        return rest('GET' if read else 'POST',path,**({'params':payload} if read else {'body':payload}),timeout=timeout)

    def op_workflow_watch(self,args):
        _only(args,('contract_version','run_id','after_seq','watch'))
        if type(args.get('contract_version')) is not int or args['contract_version']!=1 or type(args.get('watch')) is not bool:
            raise BrokerError('workflow_contract_required')
        run_id = _id_arg(args,'run_id')
        after = _int_arg(args,'after_seq',minimum=0,maximum=9223372036854775807)
        if after is None: raise BrokerError('invalid_args')
        with self._workflow_watch_lock:
            previous = self._workflow_watches.pop(run_id,None)
            if previous: previous.set()
            if args['watch']:
                stop=threading.Event(); self._workflow_watches[run_id]=stop
                threading.Thread(target=self._watch_workflow,args=(run_id,after,stop),daemon=True).start()
        return {'run_id':run_id,'watching':args['watch']}

    def _watch_workflow(self,run_id,after,stop):
        try:
            while not stop.is_set():
                try:
                    page=self.op_workflows('events',{'contract_version':1,'run_id':run_id,'after_seq':after,'limit':100,'wait':25})
                except BrokerError as exc:
                    if exc.code != 'rate_limited':
                        raise
                    delay = exc.retry_after if exc.retry_after is not None else 1
                    if stop.wait(max(0.05, delay)):
                        return
                    continue
                for event in page['events']:
                    if stop.is_set(): return
                    seq=int(event['seq'])
                    if seq<=after: continue
                    self.emit('workflow_event',event)
                    if event['kind']=='chat':
                        data=dict(event['payload']['data'])
                        name=event['payload']['name']
                        if name=='message_complete': name='complete'
                        if not event.get('conversation_id') or not event.get('request_id'):
                            raise BrokerError('invalid_workflow_chat_identity')
                        data.update(run_id=run_id,stage_key=event['stage_key'],event_seq=event['seq'],source='workflow',
                                    conversation_id=str(event['conversation_id']),request_id=str(event['request_id']))
                        self.emit(name,data)
                    after=seq
        except Exception:
            if not stop.is_set(): self.emit('workflow_connection',{'run_id':run_id,'state':'stale','reason':'workflow observation disconnected'})
        finally:
            with self._workflow_watch_lock:
                if self._workflow_watches.get(run_id) is stop: self._workflow_watches.pop(run_id,None)

    def op_work(self, operation, args):
        fields={"catalog":(), "preparation":("preparation_id",), "status":("job_id",),
                "workspace.create":("project_id","request_key"),
                "publish":("job_id","proposal_digest","expected_revision","request_key"),
                "start":("preparation_id","request_key")}[operation]
        _only(args, ("contract_version",*fields))
        if type(args.get("contract_version")) is not int or args["contract_version"]!=2:
            raise BrokerError("client_upgrade_required")
        self._require_owner_operation("work."+operation)
        for field in fields:
            if field=="expected_revision":
                if _int_arg(args,field,minimum=1,maximum=9223372036854775807) is None:raise BrokerError("invalid_args")
            else:_str_arg(args,field,required=True,max_bytes=128)
        rest=self._owner_rest if operation in {"workspace.create","publish"} else self._rest
        return rest("POST","/api/work/"+operation,body=args,timeout=self.fanout_seconds)

    def op_grants_issue(self, args):
        _only(args, ("contract_version", "proposal_id", "proposal_revision", "expected_revision", "request_key", "coverage_kind",
                     "allow_long_runs", "auto_escalate", "call_ceiling", "expires_at", "allow_extended_jobs",
                     "allow_custom_job_limit", "job_seconds_ceiling", "job_override_seconds", "job_auto_escalate"))
        self._require_owner_operation("grants.issue")
        _str_arg(args, "proposal_id", required=True, max_bytes=128)
        _str_arg(args, "request_key", required=True, max_bytes=128)
        for key in ("proposal_revision", "call_ceiling", "job_seconds_ceiling", "job_override_seconds"):
            if key in args and args[key] is not None:
                _int_arg(args, key, minimum=1, maximum=9223372036854775807)
        return self._owner_rest("POST", "/agent-services/grants", body={**args, "contract_version": 2}, timeout=self.fanout_seconds)

    def _owner_read_operation(self, operation, args, field, path):
        _only(args, ("contract_version", field) if field else ("contract_version", "cursor", "limit", "kind"))
        self._require_owner_operation(operation)
        if field:
            path += urllib.parse.quote(_id_arg(args, field), safe="")
        params = {k:v for k,v in args.items() if k != field}
        return self._owner_rest("GET", path, params={**params, "contract_version": 2}, timeout=self.fanout_seconds)

    def _owner_mutation(self, operation, args, field, path, suffix):
        _only(args, ("contract_version", field, "expected_revision", "request_key"))
        self._require_owner_operation(operation)
        resource = _id_arg(args, field)
        _str_arg(args, "request_key", required=True, max_bytes=128)
        if _int_arg(args, "expected_revision", minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError("invalid_args", "expected_revision required")
        body = {k:v for k,v in args.items() if k != field}
        return self._owner_rest("POST", path + urllib.parse.quote(resource, safe="") + suffix,
                                body={**body, "contract_version": 2}, timeout=self.fanout_seconds)

    def op_secrets_set(self, args):
        # Register the submitted secret before ANY validation/upstream failure.
        key = args.get('key') if isinstance(args, dict) else None
        if isinstance(key, str):
            _OWNER_SECRETS.add(key)
        try:
            _only(args, ('provider', 'key', 'request_key', 'contract_version'))
            if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
                raise BrokerError('invalid_args')
            capability = self._require_owner_operation('secrets.set')
            _str_arg(args, 'provider', required=True, max_bytes=64)
            if not re.fullmatch('[a-z0-9_-]{1,64}', args['provider']):
                raise BrokerError('unsupported_provider')
            if isinstance(capability.get('providers'), list) and args['provider'] not in capability['providers']:
                raise BrokerError('unsupported_provider')
            _str_arg(args, 'key', required=True, max_bytes=16384)
            _str_arg(args, 'request_key', required=True, max_bytes=128)
            if any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in key):
                raise BrokerError('invalid_key')
            if not re.fullmatch('[A-Za-z0-9_-]{1,128}', args['request_key']):
                raise BrokerError('invalid_args')
            result = self._owner_rest('POST', '/settings/secrets', body=args, timeout=self.fanout_seconds)
            if (not isinstance(result, dict) or result.get('provider') != args['provider']
                    or result.get('stored') is not True or not isinstance(result.get('fingerprint'), str)
                    or not re.fullmatch('[0-9a-f]{16}', result['fingerprint'])
                    or not isinstance(result.get('operation_id'), str)
                    or not re.fullmatch('[A-Za-z0-9_-]{1,128}', result['operation_id'])):
                raise BrokerError('invalid_receipt')
            return {k: result[k] for k in ('provider', 'stored', 'fingerprint', 'operation_id')}
        except Exception as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            code = getattr(exc, 'code', '')
            if re.fullmatch(r'gateway_http_4\d\d', code):
                code = getattr(exc, 'detail', '')
            if code not in {'invalid_args', 'invalid_receipt', 'request_key_used', 'unsupported_provider',
                            'invalid_key', 'owner_session_invalid', 'client_upgrade_required',
                            'owner_session_expired', 'owner_confirmation_required', 'unsupported_operation', 'operation_unavailable'}:
                code = 'settings_unavailable'
            raise BrokerError(code, 'Secret storage outcome requires its safe receipt; credentials were not retried') from None
        finally:
            if isinstance(key, str):
                _OWNER_SECRETS.discard(key)

    @staticmethod
    def _password_result(value):
        if (not isinstance(value, dict) or type(value.get('changed')) is not bool
                or type(value.get('restarted')) is not bool
                or (value['restarted'] and not value['changed'])):
            raise BrokerError('invalid_receipt')
        return {k: value[k] for k in ('changed', 'restarted')}

    def op_password_change(self, args):
        factors = [args.get(k) for k in ('current', 'new', 'totp')] if isinstance(args, dict) else []
        factors = [v for v in factors if isinstance(v, str)]
        _OWNER_SECRETS.update(factors)
        submitted = False
        try:
            _only(args, ('current', 'new', 'totp', 'request_key', 'contract_version'))
            if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
                raise BrokerError('invalid_args')
            for field in ('current', 'new'):
                _str_arg(args, field, required=True, max_bytes=4096)
            _str_arg(args, 'totp', required=True, max_bytes=16)
            key = _str_arg(args, 'request_key', required=True, max_bytes=128)
            if not re.fullmatch('[A-Za-z0-9_-]{1,128}', key):
                raise BrokerError('invalid_args')
            self._require_owner_operation('auth.change_password')
            used = getattr(self, '_password_request_keys', set())
            if key in used:
                raise BrokerError('request_key_used')
            used.add(key)
            self._password_request_keys = used
            submitted = True
            # Combined context+factor submission, exactly once. No owner.confirm.
            result = self._portal_owner_rest('/settings/password', args)
            return self._password_result(result)
        except Exception as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            code = getattr(exc, 'code', '')
            # Project only fixed upstream rule codes, never arbitrary response text.
            if re.fullmatch(r'gateway_http_4\d\d', code) and getattr(exc, 'detail', '') in {
                    'invalid_password', 'invalid_args', 'request_key_used', 'authentication_throttled',
                    'owner_session_invalid', 'owner_session_expired', 'owner_context_invalid'}:
                code = exc.detail
            code = {'gateway_http_429':'authentication_throttled', 'gateway_http_409':'request_key_used',
                    'gateway_http_422':'invalid_args', 'gateway_http_400':'invalid_args',
                    'gateway_http_401':'owner_context_invalid', 'gateway_http_403':'owner_context_invalid',
                    'gateway_http_404':'owner_context_invalid'}.get(code, code)
            if code not in {'invalid_args', 'invalid_password', 'invalid_receipt', 'request_key_used',
                            'authentication_throttled', 'client_upgrade_required', 'operation_unavailable',
                            'owner_portal_required', 'owner_session_invalid', 'owner_session_expired', 'owner_context_invalid'}:
                code = 'password_outcome_unknown'
            failure = BrokerError(code, 'Use settings.status with the request key; credentials were not retried')
            retry = getattr(exc, 'retry_after', None)
            if type(retry) is int and 0 <= retry <= 301:
                failure.retry_after = retry
            raise failure from None
        finally:
            if submitted:
                self._forget_owner()
            _OWNER_SECRETS.difference_update(factors)

    @staticmethod
    def _factor_prepared(value):
        if (not isinstance(value, dict) or not isinstance(value.get('challenge_id'), str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value['challenge_id'])
                or type(value.get('expires_at')) is not int or value.get('state') != 'proof_verified'):
            raise BrokerError('invalid_receipt')
        return {k: value[k] for k in ('challenge_id', 'expires_at', 'state')}

    def op_factors_binding(self, args):
        _only(args, ('contract_version',))
        if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
            raise BrokerError('invalid_args')
        self._require_owner_operation('auth.factors.binding')
        if self.identity.get('token_type') != 'portal':
            raise BrokerError('owner_portal_required')
        try:
            value = self.transport.rest('GET', '/settings/factors/binding', timeout=self.fanout_seconds,
                owner_headers={'X-Bob-Broker-Link': self._owner_link_nonce})
            fields = ('installation_id', 'lane_id', 'owner')
            if not isinstance(value, dict) or any(not isinstance(value.get(k), str)
                    or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value[k]) for k in fields):
                raise ValueError()
            if value['owner'] != self.sub or not re.fullmatch('[0-9a-f]{12}', value['lane_id']):
                raise ValueError()
            return {k: value[k] for k in fields}
        except Exception as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            raise BrokerError('recovery_unavailable') from None

    def _factor_request(self, operation, args, fields):
        factors = []
        def collect(value):
            if isinstance(value, str):
                factors.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
        collect(args)
        _OWNER_SECRETS.update(factors)
        submitted = False
        try:
            _only(args, fields)
            if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
                raise BrokerError('invalid_args')
            key = _str_arg(args, 'request_key', required=True, max_bytes=128)
            if not re.fullmatch('[A-Za-z0-9_-]{1,128}', key):
                raise BrokerError('invalid_args')
            if self.identity.get('token_type') != 'portal':
                raise BrokerError('owner_portal_required')
            self._validate_factor_args(operation, args)
            self._require_owner_operation('auth.factors.' + operation)
            used = getattr(self, '_factor_request_keys', set())
            if key in used:
                raise BrokerError('request_key_used')
            used.add(key)
            self._factor_request_keys = used
            submitted = True
            result = self._portal_owner_rest('/settings/factors/' + operation, args)
            safe = self._factor_prepared(result) if operation == 'prepare' else self._password_result(result)
            if operation == 'cancel' and safe != {'changed': False, 'restarted': False}:
                raise BrokerError('invalid_receipt')
            return safe
        except Exception as exc:
            if isinstance(exc, GatewayRateLimited):
                raise
            code = getattr(exc, 'code', '')
            code = {'gateway_http_429':'authentication_throttled', 'gateway_http_409':'recovery_conflict',
                    'gateway_http_422':'invalid_factors', 'gateway_http_400':'invalid_args',
                    'gateway_http_401':'owner_context_invalid', 'gateway_http_403':'owner_context_invalid',
                    'gateway_http_404':'owner_context_invalid'}.get(code, code)
            if code not in {'invalid_args', 'invalid_factors', 'invalid_receipt', 'request_key_used',
                            'authentication_throttled', 'client_upgrade_required', 'operation_unavailable',
                            'owner_portal_required', 'owner_context_invalid', 'recovery_conflict'}:
                code = 'recovery_outcome_unknown'
            failure = BrokerError(code, 'Use settings.status; credentials were not retried')
            retry = getattr(exc, 'retry_after', None)
            if type(retry) is int and 0 <= retry <= 301:
                failure.retry_after = retry
            raise failure from None
        finally:
            if submitted:
                self._forget_owner()
            _OWNER_SECRETS.difference_update(factors)

    @staticmethod
    def _validate_factor_args(operation, args):
        def text(key, limit=4096, source=args, pattern=None):
            item = source.get(key)
            if (not isinstance(item, str) or not item or len(item.encode('utf-8')) > limit
                    or (pattern and not re.fullmatch(pattern, item))):
                raise BrokerError('invalid_args')
        ident = '[A-Za-z0-9_-]{1,128}'
        common = {'contract_version', 'request_key'}
        if operation == 'prepare':
            pair = args.get('pair')
            pairs = {'setup': {'setup_capability', 'new_seed'},
                     'password_stick': {'password', 'old_stick', 'new_seed'},
                     'password_totp': {'password', 'totp'},
                     'totp_stick': {'totp', 'old_stick', 'new_password'}}
            if not isinstance(pair, str) or pair not in pairs or set(args) != common | {'pair', 'replacement'} | pairs[pair]:
                raise BrokerError('invalid_args')
            replacement = args['replacement']
            if not isinstance(replacement, dict) or set(replacement) != {'recovery_id', 'verifier'}:
                raise BrokerError('invalid_args')
            text('recovery_id', source=replacement, pattern=ident)
            text('verifier', source=replacement, pattern='[0-9a-f]{64}')
            for field in ('password', 'new_password'):
                if field in args:
                    text(field)
            for field, pattern in [('totp', '[0-9]{6}'), ('new_seed', '[A-Z2-7]{32}'), ('setup_capability', ident)]:
                if field in args:
                    text(field, pattern=pattern)
            if 'old_stick' in args:
                stick = args['old_stick']
                if not isinstance(stick, dict) or set(stick) != {'installation_id', 'lane_id', 'owner', 'recovery_id', 'secret'}:
                    raise BrokerError('invalid_args')
                for field in ('installation_id', 'owner', 'recovery_id'):
                    text(field, source=stick, pattern=ident)
                text('lane_id', source=stick, pattern='[0-9a-f]{12}')
                text('secret', source=stick, pattern='[A-Z2-7]{52}')
                import base64
                if base64.b32encode(base64.b32decode(stick['secret'] + '====')).decode().rstrip('=') != stick['secret']:
                    raise BrokerError('invalid_args')
        else:
            expected = common | {'challenge_id'} | ({'material_saved', 'new_totp'} if operation == 'commit' else set())
            if set(args) != expected:
                raise BrokerError('invalid_args')
            text('challenge_id', pattern=ident)
            if operation == 'commit' and (args['material_saved'] is not True or not isinstance(args['new_totp'], str)
                    or not re.fullmatch('(?:[0-9]{6})?', args['new_totp'])):
                raise BrokerError('invalid_args')

    def op_factors_prepare(self, args):
        return self._factor_request('prepare', args, ('contract_version', 'request_key', 'pair',
            'replacement', 'old_stick', 'setup_capability', 'new_seed', 'new_password', 'password', 'totp'))

    def op_factors_commit(self, args):
        return self._factor_request('commit', args, ('contract_version', 'request_key', 'challenge_id',
            'material_saved', 'new_totp'))

    def op_factors_cancel(self, args):
        return self._factor_request('cancel', args, ('contract_version', 'request_key', 'challenge_id'))

    def op_settings_status(self, args):
        # Works while gateway is down; never uses/replays an owner bearer or key.
        _only(args, ('request_key', 'operation_id'))
        if len(args) != 1:
            raise BrokerError('invalid_args')
        try:
            import socket
            import stat
            import struct
            field = next(iter(args))
            value = _str_arg(args, field, required=True, max_bytes=128)
            if not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
                raise ValueError()
            lane_id = os.environ.get('BOB_SETTINGS_ID', '')
            if not re.fullmatch('[0-9a-f]{12}', lane_id):
                raise ValueError()
            path = Path('/run/bob-settings') / lane_id / 'receipt.sock'
            for parent in (*reversed(path.parents), path):
                info = parent.lstat()
                if info.st_uid != 0 or stat.S_ISLNK(info.st_mode) or info.st_mode & (0o002 if parent == path else 0o022):
                    raise ValueError()
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(10)
                connection.connect(str(path))
                if struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != 0:
                    raise ValueError()
                connection.sendall(json.dumps({'version': 1, 'op': 'status', **args}).encode() + b'\n')
                with connection.makefile('rb') as stream:
                    raw = stream.readline(32769)
            if len(raw) > 32768 or not raw.endswith(b'\n'):
                raise ValueError()
            result = json.loads(raw)
            if result.get('phase') not in {'prepared', 'committed', 'restart-pending', 'ready', 'failed'}:
                raise ValueError()
            if not re.fullmatch('[A-Za-z0-9_-]{1,128}', result['operation_id']):
                raise ValueError()
            safe = {k: result[k] for k in ('operation_id', 'phase')}
            if safe['phase'] in {'ready', 'failed'} and isinstance(result.get('result'), dict) and 'changed' in result['result']:
                safe['result'] = self._password_result(result['result'])
            elif (safe['phase'] == 'ready' and isinstance(result.get('result'), dict)
                    and result['result'].get('state') == 'proof_verified'):
                safe['result'] = self._factor_prepared(result['result'])
            elif safe['phase'] == 'ready':
                item = result['result']
                if (item.get('stored') is not True or not isinstance(item.get('provider'), str)
                        or not re.fullmatch('[a-z0-9_-]{1,64}', item['provider'])
                        or not re.fullmatch('[0-9a-f]{16}', item['fingerprint'])
                        or item['operation_id'] != safe['operation_id']):
                    raise ValueError()
                safe['result'] = {k: item[k] for k in ('provider', 'stored', 'fingerprint', 'operation_id')}
            elif safe['phase'] == 'failed':
                safe['error'] = 'settings_failed'
            return safe
        except Exception:
            raise BrokerError('settings_receipt_unavailable', 'Safe receipt unavailable; credentials were not retried') from None

    def op_policy_get(self, args):
        _only(args, ())
        self._require_owner_operation('policy.get')
        return self._owner_rest('GET', '/settings/policy?contract_version=2', timeout=self.fanout_seconds)

    def op_policy_set(self, args):
        _only(args, ('policy', 'expected_revision', 'request_key', 'contract_version'))
        if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
            raise BrokerError('invalid_args')
        self._require_owner_operation('policy.set')
        _str_arg(args, 'request_key', required=True, max_bytes=128)
        if _int_arg(args, 'expected_revision', minimum=0, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args')
        return self._owner_rest('POST', '/settings/policy', body={**args, 'contract_version': 2}, timeout=self.fanout_seconds)

    def op_runs_followup(self, args):
        _only(args, ("run_id", "content"))
        run_id = _id_arg(args, "run_id")
        content = _text_arg(args, "content", max_bytes=20000)
        quoted = urllib.parse.quote(run_id, safe="")
        # Two sequential calls (manager turn + fresh run row) share one budget.
        deadline = time.monotonic() + self.fanout_seconds
        reply = self._rest("POST", "/agent-services/runs/%s/messages" % quoted,
                           body={"content": content}, timeout=_remaining(deadline))
        try:
            row = self._rest("GET", "/agent-services/runs/" + quoted,
                             timeout=_remaining(deadline))
        except GatewayRateLimited as exc:
            # The manager turn already succeeded; only its readback was refused.
            return {"run": None, "manager_reply": reply, "readback_failed": True,
                    "readback_error": exc.code, "readback_refusal": _error_frame(0, exc)["error"]}
        return self._with_ledger_warning({"run": row, "manager_reply": reply},
                                         self._ledger_remember(row))


    # -- Workspace tool jobs -------------------------------------------------
    # Fixed endpoint map onto the gateway's /api tool-job sub-app. The gateway
    # decides entitlement from verified scope. Decisions additionally require
    # the separate owner transport; no caller-chosen path or header is forwarded.
    # -- scoped proposal-only jobs (tool-jobs/2) ------------------------------
    def _job_path(self, args, suffix=''):
        identifier = _str_arg(args, 'job_id', required=True, max_bytes=32)
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise BrokerError('invalid_args', 'job_id must be a server job id')
        return '/api/tool-jobs/' + identifier + suffix

    def _require_jobs(self, args, operation, *, mutation=False):
        version = args.get('contract_version')
        if (version is not None and (type(version) is not int or version != 2)) or (mutation and version != 2):
            raise BrokerError('client_upgrade_required', 'tool-jobs/2 required; no reduced request was sent')
        result = self._rest('GET', '/capabilities', params={'operations': 1, 'contract_version': 2}, timeout=self.fanout_seconds)
        operations = result.get('operations') if isinstance(result, dict) else None
        advertised = operations.get(operation, {}) if isinstance(operations, dict) else {}
        if (not isinstance(advertised, dict) or not isinstance(advertised.get('contract_versions'), list)
                or 2 not in advertised['contract_versions']):
            raise BrokerError('client_upgrade_required', 'Server does not advertise ' + operation + '; no retry was sent')
        mode = 'writable' if operation in {'jobs.start', 'jobs.cancel', 'jobs.decide'} else 'readable'
        if (advertised.get('implemented') is False or advertised.get('available') is False
                or advertised.get(mode) is False or operation == 'jobs.start' and advertised.get('runnable') is False):
            raise BrokerError('operation_unavailable', str(advertised.get('reason') or operation))

        if operation == 'jobs.start' and args.get('role') == 'coder':
            caps = result.get('tool_jobs', {})
            coder = caps.get('coder', {}) if isinstance(caps, dict) else {}
            if not isinstance(coder, dict) or coder.get('worker_contract') != 'checked-edits/1' or caps.get('acceptance_revision') != 'checked-edits/1':
                raise BrokerError('client_upgrade_required', 'Server does not advertise checked-edits/1')
            if coder.get('available') is not True:
                raise BrokerError('operation_unavailable', str(coder.get('reason') or 'checked edits unavailable'))

    @staticmethod
    def _checked_acceptance(value):
        _only(value, ('criteria',))
        criteria = value.get('criteria')
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 100:
            raise BrokerError('invalid_args', 'checked criteria required')
        seen = set()
        for item in criteria:
            if not isinstance(item, dict):
                raise BrokerError('invalid_args', 'invalid checked criterion')
            _only(item, ('id', 'required', 'output_kind', 'checks'))
            cid = item.get('id')
            if (not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', cid)
                    or cid in seen or type(item.get('required', True)) is not bool or item.get('output_kind') != 'proposal'):
                raise BrokerError('invalid_args', 'invalid checked criterion')
            seen.add(cid)
            checks = item.get('checks')
            if not isinstance(checks, dict):
                raise BrokerError('invalid_args', 'registered checks required')
            _only(checks, ('required_check_ids',))
            ids = checks.get('required_check_ids')
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                    or any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', i) for i in ids)
                    or len(set(ids)) != len(ids)):
                raise BrokerError('invalid_args', 'registered checks required')
        if not any(c.get('required', True) for c in criteria):
            raise BrokerError('invalid_args', 'required acceptance missing')

    @staticmethod
    def _jobs_key(args):
        key = _str_arg(args, 'request_key', required=True, max_bytes=128)
        if not key.strip() or re.search(r'[\x00-\x1f]', key):
            raise BrokerError('invalid_args', 'invalid job request key')
        return key

    @staticmethod
    def _jobs_integer(args, field, minimum, maximum):
        value = _int_arg(args, field, minimum=minimum, maximum=9223372036854775807)
        if value is None or value > maximum:
            raise BrokerError('invalid_args', 'invalid bounded Jobs integer')
        return value

    @staticmethod
    def _jobs_start_fields(args):
        budget = args['budget']
        _only(budget, ('start_calls', 'wall_mode', 'wall_seconds', 'max_job_attempts', 'job_seconds_budget'))
        checked = args.get('role') == 'coder'
        for field in (('start_calls',) if checked and budget.get('wall_mode') == 'none' else ('start_calls', 'wall_seconds')):
            if _int_arg(budget, field, minimum=1, maximum=9223372036854775807) is None:
                raise BrokerError('invalid_args', 'finite job budget required')
        if 'max_job_attempts' in budget and _int_arg(budget, 'max_job_attempts', minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args', 'invalid job attempts')
        if budget.get('wall_mode') == 'none' and checked:
            if budget.get('wall_seconds', 'missing') is not None:
                raise BrokerError('invalid_args', 'no wall requires null seconds')
        elif budget.get('wall_mode') not in ('fixed', 'follow_budget'):
            raise BrokerError('invalid_args', 'finite job wall required')
        if checked and _int_arg(budget, 'max_job_attempts', minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args', 'finite checked job attempts required')
        if budget.get('job_seconds_budget') is not None:
            _int_arg(budget, 'job_seconds_budget', minimum=1, maximum=9223372036854775807)
        policy = args['job_policy']
        _only(policy, ('proposed_seconds', 'rationale', 'auto_escalate'))
        if _int_arg(policy, 'proposed_seconds', minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args', 'proposed job seconds required')
        _text_arg(policy, 'rationale', max_bytes=2000)
        if (checked and type(policy.get('auto_escalate')) is not bool) or (not checked and policy.get('auto_escalate') is not False):
            raise BrokerError('invalid_args', 'invalid job continuation policy')
        for field in ('intent_id', 'project_id'):
            if field in args:
                _str_arg(args, field, required=True, max_bytes=128)
        if 'job_coverage' not in args:
            return  # The server may refuse missing grant coverage; no coverage is invented.
        coverage = args['job_coverage']
        if not isinstance(coverage, dict):
            raise BrokerError('invalid_args', 'job coverage required')
        _only(coverage, ('profiles', 'workspaces', 'attachments', 'max_job_attempts', 'job_seconds_budget', 'allow_cli'))
        if coverage.get('allow_cli') is not False or _int_arg(coverage, 'max_job_attempts', minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args', 'finite non-CLI coverage required')
        if coverage.get('job_seconds_budget') is not None:
            _int_arg(coverage, 'job_seconds_budget', minimum=1, maximum=9223372036854775807)
        profiles, workspaces, attachments = (coverage.get(k) for k in ('profiles', 'workspaces', 'attachments'))
        if (not isinstance(profiles, dict) or not 1 <= len(profiles) <= 16
                or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', k)
                       or not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{64}', v) for k, v in profiles.items())):
            raise BrokerError('invalid_args', 'invalid job profiles')
        if not isinstance(workspaces, dict) or not 1 <= len(workspaces) <= 32:
            raise BrokerError('invalid_args', 'invalid job workspaces')
        for workspace, paths in workspaces.items():
            if (not isinstance(workspace, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', workspace) or not isinstance(paths, list)
                    or not 1 <= len(paths) <= 32 or any(not isinstance(p, str) or not p or len(p) > 2048
                    or p.startswith('/') or re.search(r'[\\:\x00-\x1f]', p)
                    or any(s in ('', '..') for s in p.split('/')) for p in paths)):
                raise BrokerError('invalid_args', 'invalid job paths')
        if (not isinstance(attachments, list) or not 1 <= len(attachments) <= 32
                or any(not isinstance(a, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', a) for a in attachments)
                or args['attachment'] not in attachments):
            raise BrokerError('invalid_args', 'invalid job attachments')

    def op_jobs_attachments(self, args):
        _only(args, ('contract_version',))
        self._require_jobs(args, 'jobs.attachments')
        return self._rest('GET', '/api/tool-job-attachments', params={'contract_version': 2})

    def op_jobs_list(self, args):
        _only(args, ('contract_version', 'cursor', 'limit'))
        if 'limit' in args:
            self._jobs_integer(args, 'limit', 1, 50)
        if 'cursor' in args and (not isinstance(args['cursor'], str) or not re.fullmatch(r'[0-9a-f]{32}', args['cursor'])):
            raise BrokerError('invalid_args', 'invalid job cursor')
        self._require_jobs(args, 'jobs.list')
        return self._rest('GET', '/api/tool-jobs', params={**args, 'contract_version': 2})

    def op_jobs_start(self, args):
        _only(args, ('contract_version','role','task','attachment','request_key','team','team_revision',
                     'budget','job_policy','job_coverage','intent_id','project_id',
                     'model_calls','tool_calls','wall_seconds','worker_contract','acceptance'))
        # Validate version before legacy fields so an old binary gets an honest
        # refusal; its small wall budget is never reinterpreted as a job tier.
        if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
            raise BrokerError('client_upgrade_required', 'tool-jobs/2 requires a separate native v2 command')
        if any(k in args for k in ('model_calls','tool_calls','wall_seconds')):
            raise BrokerError('invalid_args', 'v2 uses separate root budget and job_policy')
        _str_arg(args, 'request_key', required=True, max_bytes=128)
        _text_arg(args, 'task', max_bytes=16384)
        role = _str_arg(args, 'role', required=True, max_bytes=32)
        if role not in ('researcher','reviewer','proposer','coder'):
            raise BrokerError('invalid_args', 'unsupported job role')
        if role == 'coder':
            if args.get('worker_contract') != 'checked-edits/1' or not isinstance(args.get('acceptance'), dict):
                raise BrokerError('client_upgrade_required', 'coder requires checked-edits/1 and acceptance')
            self._checked_acceptance(args['acceptance'])
        elif 'worker_contract' in args or 'acceptance' in args:
            raise BrokerError('invalid_args', 'checked contract requires coder')
        _str_arg(args, 'attachment', required=True, max_bytes=64)
        _str_arg(args, 'team', required=True, max_bytes=64)
        if _int_arg(args, 'team_revision', minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args', 'team_revision required')
        if not isinstance(args.get('budget'), dict) or not isinstance(args.get('job_policy'), dict):
            raise BrokerError('invalid_args', 'budget and job_policy required')
        self._jobs_key(args)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args['attachment']):
            raise BrokerError('invalid_args', 'invalid registered attachment')
        self._jobs_start_fields(args)
        self._require_jobs(args, 'jobs.start', mutation=True)
        return self._rest('POST', '/api/tool-jobs', body=args, timeout=self.fanout_seconds)

    def op_jobs_audit(self, args):
        _only(args, ('contract_version','job_id','after_event_seq'))
        path = self._job_path(args, '/audit')
        if 'after_event_seq' in args:
            self._jobs_integer(args, 'after_event_seq', 0, 9223372036854775807)
        self._require_jobs(args, 'jobs.audit')
        return self._rest('GET', path, params={k:v for k,v in args.items() if k!='job_id'})

    def op_jobs_cancel(self, args):
        _only(args, ('contract_version','job_id','request_key'))
        path = self._job_path(args, '/cancel')
        if 'request_key' in args:
            self._jobs_key(args)
        self._require_jobs(args, 'jobs.cancel')
        # Cancellation is an intent until the audit proves descendant cleanup.
        return self._rest('POST', path, body={k:v for k,v in args.items() if k!='job_id'})

    def op_jobs_proposal(self, args):
        _only(args, ('contract_version','job_id'))
        path = self._job_path(args, '/proposal')
        self._require_jobs(args, 'jobs.proposal')
        return self._rest('GET', path, params={'contract_version':2})

    def op_jobs_decide(self, args):
        _only(args, ('contract_version','job_id','decision','proposal_digest','request_key','expected_revision'))
        if type(args.get('contract_version')) is not int or args['contract_version'] != 2:
            raise BrokerError('client_upgrade_required', 'tool-jobs/2 owner command required')
        path = self._job_path(args, '/proposal/decision')
        if args.get('decision') not in ('approve','deny'):
            raise BrokerError('invalid_args', 'decision must be approve or deny')
        sha = _str_arg(args, 'proposal_digest', required=True, max_bytes=64)
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise BrokerError('invalid_args', 'proposal_digest must be the shown sha256')
        _str_arg(args, 'request_key', required=True, max_bytes=128)
        if _int_arg(args, 'expected_revision', minimum=1, maximum=9223372036854775807) is None:
            raise BrokerError('invalid_args', 'expected_revision required')
        self._jobs_key(args)
        if self.identity.get('token_type') == 'agent':
            raise BrokerError('owner_portal_required')
        self._require_jobs(args, 'jobs.decide', mutation=True)
        # There is deliberately no ordinary-credential fallback.
        return self._owner_rest('POST', path, body={k:v for k,v in args.items() if k!='job_id'}, timeout=self.fanout_seconds)

    def op_jobs_artifact(self, args):
        _only(args, ('contract_version','job_id','filename','offset','max_bytes'))
        filename = _str_arg(args, 'filename', required=True, max_bytes=80, empty_ok=True)  # exact name, no strip
        if not re.fullmatch(r'[a-z]+-[0-9a-f]{32}\.json', filename):
            raise BrokerError('invalid_args', 'filename must be a job artifact name')
        path = self._job_path(args, '/artifacts/' + filename)
        for field,low,high in (('offset',0,4194304),('max_bytes',1,65536)):
            if field in args: self._jobs_integer(args, field, low, high)
        self._require_jobs(args, 'jobs.artifact')
        result = self._rest('GET', path, params={k:v for k,v in args.items() if k not in {'job_id','filename'}})
        # Do not relay artifact bytes outside the requested bounded readback, even
        # if a buggy server ignores max_bytes. Jobs never enter the run ledger.
        maximum, offset = int(args.get('max_bytes', 65536)), int(args.get('offset', 0))
        if (not isinstance(result, dict)
                or not {'filename', 'content', 'encoding', 'sha256', 'offset', 'next_offset', 'total_bytes', 'truncated'} <= result.keys()
                or result.get('filename') != filename
                or result.get('encoding') != 'base64'
                or not isinstance(result.get('content'), str)
                or len(result['content']) > 4 * ((maximum + 2) // 3)
                or not isinstance(result.get('sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', result['sha256'])):
            raise BrokerError('invalid_response', 'invalid bounded job artifact')
        try:
            chunk = base64.b64decode(result['content'], validate=True)
            actual = self._jobs_integer(result, 'offset', 0, 4194304)
            total = self._jobs_integer(result, 'total_bytes', 0, 9223372036854775807)
            end = offset + len(chunk)
            if len(chunk) > maximum or actual != offset or end > total:
                raise ValueError()
            if result.get('truncated') is True:
                if self._jobs_integer(result, 'next_offset', 0, 4194304) != end or not offset < end < total:
                    raise ValueError()
            elif result.get('truncated') is not False or result.get('next_offset') is not None or end != total:
                raise ValueError()
        except (ValueError, BrokerError):
            raise BrokerError('invalid_response', 'invalid bounded job artifact') from None
        return {key: result[key] for key in ('filename', 'content', 'encoding', 'sha256', 'offset',
                                           'next_offset', 'total_bytes', 'truncated')}




def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return items
    return []


def _coverage_note(payload):
    counts = payload.get("counts_by_kind") if isinstance(payload.get("counts_by_kind"), dict) else {}
    excluded = payload.get("unattributed_events_excluded")
    return ("Server-authoritative central memory: %s family paragraphs, %s explicitly attributed "
            "Bob events, %s derived facts; complete_corpus=%s; search=%s; %s unattributed events "
            "excluded." % (
                counts.get("family_memory", "unknown"),
                counts.get("bob_event", "unknown"),
                counts.get("bob_fact", "unknown"),
                payload.get("complete_corpus"),
                payload.get("search_mode") or "unknown",
                excluded if excluded is not None else "unknown",
            ))


# ---------------------------------------------------------------------------
# Startup: token file, identity binding, server probe
# ---------------------------------------------------------------------------
def load_token(path):
    try:
        info = os.lstat(path)
    except OSError:
        raise BrokerError("token_unavailable", "token file is missing or unreadable") from None
    if stat.S_ISLNK(info.st_mode):
        raise BrokerError("token_file_insecure", "token file must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise BrokerError("token_file_insecure", "token file is not a regular file")
    if info.st_uid != os.getuid():
        raise BrokerError("token_file_insecure", "token file is not owned by this user")
    if info.st_mode & 0o077:
        raise BrokerError("token_file_insecure", "token file mode must be 0600 (group/other bits are set)")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(8192)
    except OSError:
        raise BrokerError("token_unavailable", "token file could not be read") from None
    token = raw.strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        raise BrokerError("token_invalid", "token file does not hold a single bearer token")
    return token


def decode_claims(token):
    """Read the token's own claims. The signature is never trusted here: the gateway
    validates it on every call, and /auth/status confirms liveness at startup."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("not a JWT")
        segment = parts[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure is the same typed refusal
        raise BrokerError("token_invalid", "agent token is not a decodable JWT") from None
    if not isinstance(claims, dict):
        raise BrokerError("token_invalid", "agent token claims are not an object")
    return claims


def bind_identity(transport, claims, gateway):
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise BrokerError("token_invalid", "token has no subject")
    expires = claims.get("exp")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise BrokerError("token_invalid", "token has no expiry")
    if float(expires) <= time.time():
        raise BrokerError("token_expired", "agent token is expired")
    if claims.get("token_type") not in {"agent", "portal"}:
        raise BrokerError("token_type_unsupported",
                          "this broker requires a scoped agent or portal token")
    status = transport.rest("GET", "/auth/status")
    if not isinstance(status, dict) or status.get("authenticated") is not True:
        raise BrokerError("token_rejected", "gateway did not accept the token")
    if status.get("user") != sub:
        raise BrokerError("identity_mismatch", "gateway subject differs from token subject")
    scope = claims.get("scope") if isinstance(claims.get("scope"), dict) else {}
    faces = claims.get("faces") if isinstance(claims.get("faces"), list) else []
    return {
        "sub": sub,
        "token_type": claims.get("token_type"),
        "gateway": gateway,
        "faces": [str(item) for item in faces if isinstance(item, str)],
        "may_touch": [str(item) for item in _as_list(scope.get("may_touch")) if isinstance(item, str)],
        "may_not_touch": [str(item) for item in _as_list(scope.get("may_not_touch")) if isinstance(item, str)],
        "expires_at": _epoch_iso(expires),
    }


def probe_server(transport):
    """The gateway publishes no build version or boot time; report nulls honestly."""
    try:
        health = transport.rest("GET", "/health")
    except BrokerError:
        return {"service": None, "status": "unreachable", "boot": None, "version": None}
    status = health if isinstance(health, dict) else {}
    return {"service": status.get("service"), "status": status.get("status"),
            "boot": status.get("boot"), "version": status.get("version")}


# ---------------------------------------------------------------------------
# Stdio loop
# ---------------------------------------------------------------------------
class _Oversize(Exception):
    pass


def read_request_line(stream, limit):
    """Return one line (bytes, newline kept) or None at EOF; raise _Oversize."""
    raw = stream.readline(limit + 2)
    if not raw:
        return None
    if raw.endswith(b"\n"):
        if len(raw) - 1 > limit:
            raise _Oversize()
        return raw
    if len(raw) >= limit + 2:
        drained = 0
        while True:
            chunk = stream.readline(65536)
            if not chunk or chunk.endswith(b"\n"):
                break
            drained += len(chunk)
            if drained > 8 * 1024 * 1024:
                break
        raise _Oversize()
    return raw


def _safe_reply(sink, frame, rid):
    try:
        sink.reply(frame)
    except BrokerError as exc:
        try:
            sink.reply(_error_frame(rid, exc))
        except Exception:  # noqa: BLE001 - the link is gone; nothing else to say
            pass
    except Exception:  # noqa: BLE001 - one unencodable reply must not end the broker
        try:
            sink.reply({"id": rid, "ok": False,
                        "error": {"code": "internal_error",
                                  "detail": "the reply could not be encoded"}})
        except Exception:  # noqa: BLE001
            pass


def serve(broker, sink, stream):
    while True:
        try:
            raw = read_request_line(stream, MAX_REQUEST_BYTES)
        except _Oversize:
            _safe_reply(sink, _error_frame(0, BrokerError(
                "request_oversize", "request line exceeds %d bytes" % MAX_REQUEST_BYTES)), 0)
            continue
        if raw is None:
            break
        if not raw.strip():
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _safe_reply(sink, _error_frame(0, BrokerError("invalid_json", "request line is not UTF-8")), 0)
            continue
        try:
            frame = json.loads(text)
        except ValueError:
            _safe_reply(sink, _error_frame(0, BrokerError("invalid_json", "request line is not JSON")), 0)
            continue
        rid = frame.get("id") if isinstance(frame, dict) else None
        safe_id = rid if (isinstance(rid, int) and not isinstance(rid, bool) and rid >= 0) else 0
        try:
            result = broker.handle(frame)
        except BrokerError as exc:
            _safe_reply(sink, _error_frame(safe_id, exc), safe_id)
            continue
        except Exception as exc:  # noqa: BLE001 - never leak internals or die on one frame
            _safe_reply(sink, _error_frame(safe_id, BrokerError("internal_error",
                                                                type(exc).__name__)), safe_id)
            continue
        _safe_reply(sink, {"id": safe_id, "ok": True, "value": result.value}, safe_id)
        if result.release is not None:
            result.release()


def _thread_spawn(target):
    thread = threading.Thread(target=target, name="portal-chat", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Offline selftest (no network, no token)
# ---------------------------------------------------------------------------
def _selftest():
    checks = 0

    def check(condition, label):
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(label)

    pieces = split_utf8("é" * 40000, CHUNK_TEXT_BYTES)
    check(all(len(piece.encode("utf-8")) <= CHUNK_TEXT_BYTES for piece in pieces), "chunk bound")
    check("".join(pieces) == "é" * 40000, "chunk round-trip")

    set_secret("SELFTEST-SECRET")
    check(scrub("x SELFTEST-SECRET y") == "x [redacted] y", "secret scrub")
    check(BrokerError("e", "SELFTEST-SECRET").detail == "[redacted]", "error detail scrub")
    set_secret("")

    kept, excluded, unattributed = attribute_rows(
        [{"user_id": "admin"}, {"user_id": "other"}, {}, "junk"], "admin")
    check(len(kept) == 1 and excluded == 2 and unattributed == 1, "ownership attribution")
    check(not rows_carry_owner([{"id": "c1"}]) and rows_carry_owner([{"user_id": "admin"}]), "owner detection")
    check(scope_note(SCOPE_GATEWAY_ACCOUNT, False, 0, 3)["row_owner_attributable"] is False, "scope note")
    check(clamp_text("é" * 10, 7) == ("ééé", True), "text clamp")
    marker, marker_bytes = metadata_within({"blob": "x" * 9000})
    check(marker["truncated"] is True and marker_bytes == 0, "metadata trim")
    check(metadata_within({"a": 1})[1] == 7, "metadata size")

    check(_limit_arg({"limit": 999}, "limit", default=20, maximum=50) == 50, "limit clamp")

    claims = decode_claims("h." + base64.urlsafe_b64encode(
        json.dumps({"sub": "admin", "exp": 9999999999, "token_type": "agent"}).encode()).decode().rstrip("=") + ".s")
    check(claims["sub"] == "admin", "claim decode")

    try:
        read_request_line(__import__("io").BytesIO(b"x" * (MAX_REQUEST_BYTES + 10) + b"\n"), MAX_REQUEST_BYTES)
        check(False, "oversize line should raise")
    except _Oversize:
        check(True, "oversize line")

    check(read_request_line(__import__("io").BytesIO(b""), MAX_REQUEST_BYTES) is None, "eof")
    print(json.dumps({"selftest": "ok", "checks": checks, "version": VERSION}))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="portal-broker.py",
        description="LANE S stdio broker for the Bob portal (CONTRACT §3 ops).")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                        help="0600 file holding the agent bearer (default: %(default)s)")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY,
                        help="loopback gateway base URL (default: %(default)s)")
    parser.add_argument("--portal-approvals", action="store_true", default=True,
                        help="enable approvals.decide (server-side ownership guard is live; "
                             "the gateway still decides and can refuse)")
    parser.add_argument("--no-portal-approvals", dest="portal_approvals", action="store_false",
                        help="refuse approvals.decide in the broker (client-side gate only)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="per-request REST timeout in seconds (default: %(default)s)")
    parser.add_argument("--chat-idle-timeout", type=float, default=900.0,
                        help="websocket read idle timeout in seconds (default: %(default)s)")
    parser.add_argument("--fanout-timeout", type=float, default=FANOUT_SECONDS,
                        help="wall-clock budget in seconds for a multi-call read fanout (default: %(default)s)")
    parser.add_argument("--max-stream-seconds", type=float, default=STREAM_SECONDS,
                        help="broker-side active work cap in seconds on one chat stream; ends with stream_deadline "
                             "(default: %(default)s)")

    parser.add_argument("--selftest", action="store_true",
                        help="run offline self-checks and exit")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def normalize_gateway(value):
    """Only a loopback gateway is allowed: the bearer never leaves this host."""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "ws") or (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise BrokerError("gateway_not_loopback",
                          "the bearer stays on this host: --gateway must be loopback")
    return "http://" + value.split("://", 1)[-1].rstrip("/")


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.selftest:
        return _selftest()

    if not args.token_file or not args.token_file.strip():
        print("Set BOB_BROKER_TOKEN_FILE or pass --token-file to select the token file.", file=sys.stderr)
        return 2

    try:
        gateway = normalize_gateway(args.gateway)
        token = load_token(args.token_file)
        set_secret(token)
        claims = decode_claims(token)
        transport = LiveTransport(gateway, token, timeout=args.timeout, chat_idle=args.chat_idle_timeout,
                                  token_file=args.token_file)
        identity = bind_identity(transport, claims, gateway)
        server = probe_server(transport)
    except BrokerError as exc:
        payload = {"code": exc.code, "detail": exc.detail, **_rate_limit_fields(exc)}
        print(json.dumps({"ok": False, "error": payload}), file=sys.stderr)
        return 3

    sink = JsonLineSink(sys.stdout.buffer)
    broker = Broker(transport, identity, sink, allow_approvals=args.portal_approvals,
                    spawn=_thread_spawn, server=server, chat_idle=args.chat_idle_timeout,
                    fanout_seconds=args.fanout_timeout,
                    stream_seconds=args.max_stream_seconds)
    try:
        serve(broker, sink, sys.stdin.buffer)
    except KeyboardInterrupt:
        pass
    finally:
        broker.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
