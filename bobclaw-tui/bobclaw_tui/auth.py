"""BoBClaw TUI — gateway login (identity fix: no more self-minted tokens).

The gateway scopes every route by ``user_id`` and login always yields the ``admin``
subject, so the TUI must authenticate like the CLI (``scripts/bobclaw_cli.py``) instead
of minting its own JWT: password + TOTP from the gateway's ``.secrets/bobclaw.env`` →
``POST /auth/login`` → ``{access_token, refresh_token}``.

Flow (mirrors the CLI's cached-token-first pattern, plus refresh):

  1. Try the cached token file (``.secrets/tui-token.json``) first — validate the
     access token with a cheap authed GET (``/conversations``). TOTP replay protection
     rejects a second login inside the same 30s step, so reuse beats re-login.
  2. On a miss/invalid (401) token, ``POST /auth/refresh`` with the cached
     ``refresh_token`` before falling back to a full ``/auth/login``.
  3. Cache the token pair as JSON, written ``0600`` at creation (``os.open`` mode, not
     a post-hoc ``chmod``, so there's no race window).

The cache path defaults to ``.secrets/tui-token.json`` at the repo root and is
overridable via the ``cache_path`` argument.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import aiohttp
import pyotp
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".secrets" / "bobclaw.env"
DEFAULT_TOKEN_CACHE = _REPO_ROOT / ".secrets" / "tui-token.json"


def _base_url(gateway: str) -> str:
    return gateway if "://" in gateway else f"http://{gateway}"


def _load_credentials(env_file: Path | str) -> tuple[str, str]:
    """Read BOBCLAW_PASSWORD / TOTP_SECRET from the gateway env file.

    The file is shared with Windows tooling and may carry CRLF endings, so normalize
    before handing it to ``dotenv_values``.
    """
    path = Path(env_file)
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:
        raise SystemExit(f"cannot read gateway env file {path}: {exc}")
    vals = dotenv_values(stream=io.StringIO(text))
    password = vals.get("BOBCLAW_PASSWORD") or ""
    totp_secret = vals.get("TOTP_SECRET") or ""
    if not password:
        raise SystemExit(f"BOBCLAW_PASSWORD missing from {path}")
    if not totp_secret:
        raise SystemExit(f"TOTP_SECRET missing from {path}")
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
            raise SystemExit(f"login failed {r.status}: {await r.text()}")
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
    """Cheap authed GET — same validity probe as the CLI."""
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

    Cached access token first (validated via ``/conversations``); on a miss or 401,
    ``/auth/refresh`` with the cached refresh token; only then a full ``/auth/login``.
    The resulting pair is cached (0600) for the next run / mid-session 401 retry.
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
