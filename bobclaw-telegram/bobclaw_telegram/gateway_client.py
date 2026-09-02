"""bobclaw-telegram — gateway authentication.

Self-contained copy of the cached-token flow from
``bobclaw-tui/bobclaw_tui/auth.py`` (same repo); keep the two in step. The
bot authenticates like the TUI/CLI: password + TOTP from the gateway's
``.secrets/bobclaw.env`` → ``POST /auth/login`` → ``{access_token,
refresh_token}`` — never the TOTP seed directly, never a self-minted token.

Flow:

  1. Try the cached token file (``.secrets/telegram-refresh.json``) first —
     validate the access token with a cheap authed GET (``/conversations``).
     TOTP replay protection rejects a second login inside the same 30s step,
     so reuse beats re-login.
  2. On a miss/invalid (401) token, ``POST /auth/refresh`` with the cached
     ``refresh_token`` before falling back to a full ``/auth/login``.
  3. Cache the token pair as JSON, written ``0600`` at creation (``os.open``
     mode + ``os.fchmod``, so there's no chmod race window).
"""
from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import aiohttp
import pyotp

logger = logging.getLogger(__name__)
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".secrets" / "bobclaw.env"
DEFAULT_TOKEN_CACHE = _REPO_ROOT / ".secrets" / "telegram-refresh.json"


class AuthError(Exception):
    """Credential/login failures — must propagate as a real Exception (PTB tasks
    swallow BaseException subclasses like SystemExit silently).
    """


class GatewayError(RuntimeError):
    """A gateway call failed (non-2xx, transport error, or an error frame)."""

    def __init__(self, message: str, code: str = "gateway_error") -> None:
        super().__init__(message)
        self.code = code


def _base_url(gateway: str) -> str:
    """Resolve the gateway base URL. Loopback-only for plaintext http — credentials
    and bearer tokens ride these calls, so a non-loopback http target is a config
    error, not a warning (audit 3A)."""
    base = gateway if "://" in gateway else f"http://{gateway}"
    from urllib.parse import urlparse

    parsed = urlparse(base)
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise AuthError(
            f"refusing plaintext http to non-loopback gateway host {parsed.hostname!r} "
            "(use https or 127.0.0.1)"
        )
    return base


def _ws_base(gateway: str) -> str:
    base = _base_url(gateway)
    return base.replace("http://", "ws://").replace("https://", "wss://")


def _load_credentials(env_file: Path | str) -> tuple[str, str]:
    """Read BOBCLAW_PASSWORD / TOTP_SECRET from the gateway env file.

    The file is shared with Windows tooling and may carry CRLF endings, so
    normalize before handing it to ``dotenv_values``.
    """
    path = Path(env_file)
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:
        raise AuthError(f"cannot read gateway env file {path}: {exc}")
    vals = dotenv_values(stream=io.StringIO(text))
    password = vals.get("BOBCLAW_PASSWORD") or ""
    totp_secret = vals.get("TOTP_SECRET") or ""
    if not password:
        raise AuthError(f"BOBCLAW_PASSWORD missing from {path}")
    if not totp_secret:
        raise AuthError(f"TOTP_SECRET missing from {path}")
    return password, totp_secret


def _read_cache(cache_path: Path | str) -> dict:
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache_path: Path | str, tokens: dict) -> None:
    """Persist the token pair with mode 0600 set at creation (no chmod race window)."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # fd-based (race-free): normalize a pre-existing loose file
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tokens, f)


async def _login(session, base: str, env_file: Path | str) -> dict:
    password, totp_secret = _load_credentials(env_file)
    async with session.post(
        f"{base}/auth/login",
        json={"password": password, "totp_code": pyotp.TOTP(totp_secret).now()},
    ) as r:
        if r.status != 200:
            raise AuthError(f"login failed {r.status}: {await r.text()}")
        data = await r.json()
    return {"access_token": data["access_token"], "refresh_token": data["refresh_token"]}


async def _refresh(session, base: str, refresh_token: str) -> dict | None:
    """Rotate the token pair via /auth/refresh; None when the refresh token is dead."""
    try:
        async with session.post(
            f"{base}/auth/refresh", json={"refresh_token": refresh_token}
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except aiohttp.ClientError:
        return None
    return {"access_token": data["access_token"], "refresh_token": data["refresh_token"]}


async def _is_valid(session, base: str, access_token: str) -> bool:
    """Cheap authed GET — same validity probe as the CLI/TUI."""
    try:
        async with session.get(
            f"{base}/conversations",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as r:
            return r.status == 200
    except aiohttp.ClientError:
        return False


async def get_access_token(
    gateway: str,
    *,
    cache_path: Path | str = DEFAULT_TOKEN_CACHE,
    env_file: Path | str = _ENV_FILE,
    session=None,
) -> str:
    """Return a valid gateway access token.

    Cached access token first (validated via ``/conversations``); on a miss or
    401, ``/auth/refresh`` with the cached refresh token; only then a full
    ``/auth/login``. The resulting pair is cached (0600) for the next run /
    mid-session 401 retry.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        base = _base_url(gateway)
        cached = _read_cache(cache_path)
        access = cached.get("access_token") or ""
        if access and await _is_valid(session, base, access):
            return access
        refresh_token = cached.get("refresh_token") or ""
        if refresh_token:
            tokens = await _refresh(session, base, refresh_token)
            if tokens is not None:
                _write_cache(cache_path, tokens)
                return tokens["access_token"]
        tokens = await _login(session, base, env_file)
        _write_cache(cache_path, tokens)
        return tokens["access_token"]
    finally:
        if own_session:
            await session.close()


# ── Turn execution (Task 2) ──────────────────────────────────────────────
# Mirrors the dispatch pattern from bob-next's _workspace/dispatch.py:
# auth (Bearer on the upgrade), open /ws/chat, send one message frame with
# the conversation_id, stream chunk frames until message_complete.

# A turn can park long on a core approval wait (accepted 3A failure mode), so:
# no overall cap, generous per-read cap — same rationale as the gateway relay.
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=600)


async def create_conversation(
    gateway: str,
    *,
    title: str = "Telegram",
    face_id: str | None = None,
    token: str | None = None,
    cache_path: Path | str = DEFAULT_TOKEN_CACHE,
    env_file: Path | str = _ENV_FILE,
    session=None,
) -> str:
    """POST /conversations and return the new conversation id."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        base = _base_url(gateway)
        token = token or await get_access_token(
            gateway, cache_path=cache_path, env_file=env_file, session=session
        )
        async with session.post(
            f"{base}/conversations",
            json={"title": title, **({"face_id": face_id} if face_id else {})},
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            if r.status not in (200, 201):
                raise GatewayError(
                    f"create conversation failed {r.status}: {await r.text()}"
                )
            return str((await r.json())["id"])
    finally:
        if own_session:
            await session.close()


async def stream_turn(
    gateway: str,
    conversation_id: str,
    content: str,
    *,
    token: str | None = None,
    cache_path: Path | str = DEFAULT_TOKEN_CACHE,
    env_file: Path | str = _ENV_FILE,
    session=None,
    on_approval: Callable[[dict], Awaitable[None]] | None = None,
    face_id: str | None = None,
) -> AsyncIterator[str]:
    """Run one BoB turn over /ws/chat, yielding assistant text chunks.

    Sends ``{"type": "message", "conversation_id", "content"}``, yields each
    ``chunk`` frame's content, and returns on ``message_complete`` /
    ``generation_stopped`` or a closed socket. A gateway ``error`` frame
    raises :class:`GatewayError`. ``approval_request`` frames invoke
    *on_approval* (once per frame) when given — 3A is notify-only, so the
    turn simply parks until the approval is decided elsewhere (accepted
    failure mode).
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=_STREAM_TIMEOUT)
    try:
        token = token or await get_access_token(
            gateway, cache_path=cache_path, env_file=env_file, session=session
        )
        async with session.ws_connect(
            f"{_ws_base(gateway)}/ws/chat",
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=30,
        ) as ws:
            await ws.send_json({
                "type": "message",
                "conversation_id": conversation_id,
                "content": content,
                **({"face_id": face_id} if face_id else {}),
            })
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        # A mid-stream socket drop is NOT a clean end — without this the
                        # truncated reply looks complete (audit 3A task-2).
                        raise GatewayError("chat socket closed mid-stream", "stream_closed")
                    continue
                data = json.loads(msg.data)
                frame_type = data.get("type")
                if frame_type == "chunk":
                    yield str(data.get("content") or "")
                elif frame_type == "approval_request" and on_approval is not None:
                    try:
                        await on_approval(data)
                    except Exception:  # noqa: BLE001 — a notify failure must
                        # never abort the turn's frame loop (audit 3A task-3)
                        logger.exception("approval notify hook failed")
                elif frame_type == "error":
                    raise GatewayError(
                        str(data.get("message") or "gateway error"),
                        str(data.get("code") or "gateway_error"),
                    )
                elif frame_type in ("message_complete", "generation_stopped"):
                    return
    finally:
        if own_session:
            await session.close()


# ── Approvals (Task 3, notify-only) ───────────────────────────────────────
# Mirrors the REST poll pattern from bobclaw-tui/bobclaw_tui/pollers.py —
# same authed GET on /approvals?status=pending the TUI's Approvals pane uses.


async def list_pending_approvals(
    gateway: str,
    *,
    token: str | None = None,
    cache_path: Path | str = DEFAULT_TOKEN_CACHE,
    env_file: Path | str = _ENV_FILE,
    session=None,
) -> list[dict]:
    """GET ``/approvals?status=pending`` and return the ``items`` list.

    Notify-only in 3A — there is deliberately no decide call here. Raises
    :class:`GatewayError` on a non-200 so the poller can log and skip the
    tick; transport errors propagate as ``aiohttp.ClientError`` (the poller
    is fail-open on both).
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        base = _base_url(gateway)
        token = token or await get_access_token(
            gateway, cache_path=cache_path, env_file=env_file, session=session
        )
        async with session.get(
            f"{base}/approvals?status=pending",
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            if r.status != 200:
                raise GatewayError(f"list approvals failed {r.status}: {await r.text()}")
            data = await r.json()
        # A 200 with an unexpected shape must NOT read as "nothing pending" —
        # a notify-only safety feature that silently reports zero is worse than
        # a loud failure (audit 3A task-3).
        if not isinstance(data, dict) or "items" not in data:
            raise GatewayError(f"unexpected approvals payload shape: {str(data)[:200]}", "bad_payload")
        return [i for i in data["items"] if isinstance(i, dict)]
    finally:
        if own_session:
            await session.close()


async def stop_turn(
    gateway: str,
    conversation_id: str,
    *,
    token: str | None = None,
    cache_path: Path | str = DEFAULT_TOKEN_CACHE,
    env_file: Path | str = _ENV_FILE,
    session=None,
) -> bool:
    """Send ``stop_generation`` WITH the conversation_id (required post-1A).

    The gateway keys active streams by (user, conversation), so a fresh
    /ws/chat connection can cancel a turn started on another. Returns True
    when the gateway confirms ``generation_stopped``.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        token = token or await get_access_token(
            gateway, cache_path=cache_path, env_file=env_file, session=session
        )
        async with session.ws_connect(
            f"{_ws_base(gateway)}/ws/chat",
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=30,
        ) as ws:
            await ws.send_json({
                "type": "stop_generation",
                "conversation_id": conversation_id,
            })
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        return False
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "generation_stopped":
                    return True
                if data.get("type") == "error":
                    return False  # e.g. no_active_generation
            return False
    finally:
        if own_session:
            await session.close()
