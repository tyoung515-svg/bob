"""Lane-only ChatGPT credentials and bounded, serialized refresh-token rotation.

Refresh/adoption behavior derived from Hermes (MIT, Nous Research).
No host CLI home, process token environment, or repository dotenv is consulted.
"""
from __future__ import annotations

from core.worker import mode as execution_mode
from core.worker import client as worker_client

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import hashlib
import os
import stat
import tempfile
import time

import httpx

from core.lane_config import codex_auth_path, get_lane, read_codex_auth

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class CodexProviderError(RuntimeError):
    """Sanitized provider failure; status is understood by Bob's error receipts."""

    def __init__(self, detail: str, status: int | None = None, *, code: str | None = None, retry_after: str | None = None, failure=None):
        self.status = status
        self.code, self.retry_after = code, retry_after
        self.failure = failure
        self.event_type = getattr(failure, 'event_type', None)
        self.provider_code = getattr(failure, 'provider_code', None)
        self.provider_type = getattr(failure, 'provider_type', None)
        self.incomplete_reason = getattr(failure, 'incomplete_reason', None)
        self.resets_at = getattr(failure, 'resets_at', None)
        super().__init__(f"openai_codex: {detail}")


def jwt_claims(token: str) -> dict:
    """Decode expiry/account hints only, without claiming to verify a JWT."""
    try:
        payload = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


@dataclass(frozen=True)
class Credentials:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    account: str = field(repr=False)

    def expiring(self, now: float, skew: float = 60) -> bool:
        exp = jwt_claims(self.access).get("exp")
        return not self.access or (isinstance(exp, (int, float)) and exp <= now + skew)


class LaneCodexAuth:
    def __init__(self, lane=None, *, clock=time.time, lock_timeout: float = 30):
        self.lane = lane if lane is not None else get_lane()
        self.clock = clock
        self.lock_timeout = lock_timeout

    def __repr__(self):
        return "LaneCodexAuth(<lane credentials redacted>)"

    def load(self) -> Credentials:
        return self._credentials(read_codex_auth(self.lane))

    @staticmethod
    def _credentials(auth: dict) -> Credentials:
        tokens = auth.get("tokens") or {}
        if not isinstance(tokens, dict):
            tokens = {}
        access, refresh = tokens.get("access_token") or "", tokens.get("refresh_token") or ""
        if not isinstance(access, str) or not isinstance(refresh, str) or not (access or refresh):
            raise CodexProviderError("ChatGPT credentials missing; sign in again")
        claims = jwt_claims(access).get("https://api.openai.com/auth") or {}
        account = tokens.get("account_id") or (claims.get("chatgpt_account_id") if isinstance(claims, dict) else "")
        return Credentials(access, refresh, account if isinstance(account, str) else "")

    @asynccontextmanager
    async def _lock(self):
        # Local offline evidence does not establish Codex CLI's auth.lock ABI.
        # Use a separate advisory Bob lock; always re-read/adopt CLI rotations.
        if execution_mode() == "legacy":
            path = codex_auth_path(self.lane).with_name("auth.bob.lock")
        else:
            path = (self.lane.root / 'auth.bob.lock' if self.lane.values.get('BOB_WORKER_ID')
                    else codex_auth_path(self.lane).with_name("auth.bob.lock"))
        if self.lane.owned_path(str(path)) != path:
            raise CodexProviderError("auth lock must belong to this lane")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise CodexProviderError("cannot open auth lock; retry later") from None
        locked = False
        try:
            if execution_mode() == "worker":
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != 'nt' and info.st_uid != os.geteuid()):
                    raise CodexProviderError('unsafe auth lock')
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CodexProviderError("auth lock timeout; retry later") from None
                    await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            if locked:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _save(self, payload: dict, initial: Credentials) -> Credentials:
        # CLI does not share our lock: re-read again before merging/writing.
        auth = read_codex_auth(self.lane)
        current = self._credentials(auth)
        if current != initial:
            return current
        if execution_mode() == "worker":
            if self.lane.values.get('BOB_WORKER_ID'):
                from core.worker.auth_file import directory, read_at
                from core.worker import client as worker_client
                path = codex_auth_path(self.lane)
                try:
                    with directory(path.parent) as dfd:
                        raw = read_at(dfd, int(self.lane.values['BOB_WORKER_UID']))
                    snapshot = json.loads(raw)
                    if self._credentials(snapshot) != initial:
                        return self._credentials(snapshot)
                    for key in ('access_token', 'refresh_token', 'id_token'):
                        if isinstance(payload.get(key), str) and payload[key]:
                            snapshot['tokens'][key] = payload[key]
                    snapshot['last_refresh'] = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace('+00:00', 'Z')
                    worker_client.exchange(worker_client.request('auth-replace', input=json.dumps({
                        'expected': hashlib.sha256(raw).hexdigest(), 'auth': snapshot}), timeout=10))
                    return self.load()
                except (OSError, ValueError, RuntimeError):
                    raise CodexProviderError('could not persist shared credentials; retry later') from None
        tokens = auth["tokens"]
        for key in ("access_token", "refresh_token", "id_token"):
            if isinstance(payload.get(key), str) and payload[key]:
                tokens[key] = payload[key]
        auth["last_refresh"] = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")
        path = codex_auth_path(self.lane)
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix=".auth-bob-", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                json.dump(auth, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            name = None
        except (OSError, ValueError):
            raise CodexProviderError("could not persist refreshed credentials; sign in again") from None
        finally:
            if name is not None:
                os.unlink(name)
        return self.load()

    async def credentials(self, client: httpx.AsyncClient, *, previous: Credentials | None = None) -> Credentials:
        loaded = previous if previous is not None else self.load()
        if previous is None and not loaded.expiring(self.clock()):
            return loaded
        async with self._lock():
            current = self.load()
            if current != loaded:
                return current
            if not current.refresh:
                raise CodexProviderError("ChatGPT session expired; sign in again", 401)
            try:
                async with asyncio.timeout(20):
                    response = await client.post(TOKEN_URL, json={
                        "grant_type": "refresh_token", "refresh_token": current.refresh,
                        "client_id": CLIENT_ID}, timeout=20)
            except (httpx.HTTPError, TimeoutError):
                raise CodexProviderError("token refresh transport failed; retry later") from None
            if response.status_code == 429:
                raise CodexProviderError("token endpoint quota exceeded", 429)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if response.status_code >= 400:
                error = payload.get("error")
                code = error.get("code") or error.get("type") if isinstance(error, dict) else error
                if (isinstance(code, str) and code in {"refresh_token_reused", "invalid_grant", "invalid_token"}) or response.status_code == 401:
                    peer = self.load()
                    if peer != current:
                        return peer
                    raise CodexProviderError("ChatGPT session expired; sign in again", response.status_code)
                raise CodexProviderError("token refresh failed", response.status_code)
            if not all(isinstance(payload.get(k), str) and payload[k] for k in ("access_token", "refresh_token")):
                raise CodexProviderError("invalid token refresh response; sign in again")
            return self._save(payload, current)
