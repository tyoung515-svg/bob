"""BoBClaw TUI — gateway-login tests (identity fix; hermetic, all HTTP mocked)."""
from __future__ import annotations

import asyncio
import importlib
import json
import stat

import pytest

from bobclaw_tui import auth

_ENV = "BOBCLAW_PASSWORD=hunter2\nTOTP_SECRET=JBSWY3DPEHPK3PXP\n"


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    """Records calls and serves queued responses per (method, path)."""

    def __init__(self, routes: dict):
        self._routes = routes
        self.calls: list[tuple[str, str]] = []

    def _handle(self, method, url, **kw):
        path = url.split("/", 3)[-1] if "://" in url else url
        path = "/" + path.lstrip("/")
        self.calls.append((method, path))
        handler = self._routes.get((method, path))
        assert handler is not None, f"unexpected {method} {path}"
        resp = handler(kw) if callable(handler) else handler
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, **kw):
        return self._handle("POST", url, **kw)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env_file(tmp_path):
    p = tmp_path / "bobclaw.env"
    p.write_text(_ENV)
    return p


def test_login_obtains_token_pair_and_cache_file_is_0600(tmp_path, env_file):
    cache = tmp_path / "tui-token.json"
    pair = {"access_token": "acc-1", "refresh_token": "ref-1", "token_type": "Bearer"}
    s = _FakeSession({("POST", "/auth/login"): _Resp(200, pair)})

    token = _run(auth.get_access_token(
        "127.0.0.1:7836", cache_path=cache, env_file=env_file, session=s))

    assert token == "acc-1"
    assert s.calls == [("POST", "/auth/login")]
    assert json.loads(cache.read_text()) == {"access_token": "acc-1", "refresh_token": "ref-1"}
    mode = stat.S_IMODE(cache.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_valid_cached_token_reused_without_login(tmp_path, env_file):
    cache = tmp_path / "tui-token.json"
    cache.write_text(json.dumps({"access_token": "acc-old", "refresh_token": "ref-old"}))
    s = _FakeSession({("GET", "/conversations"): _Resp(200, {"items": []})})

    token = _run(auth.get_access_token(
        "127.0.0.1:7836", cache_path=cache, env_file=env_file, session=s))

    assert token == "acc-old"
    assert s.calls == [("GET", "/conversations")]  # no /auth/login, no /auth/refresh


def test_expired_access_token_refreshes_before_login(tmp_path, env_file):
    cache = tmp_path / "tui-token.json"
    cache.write_text(json.dumps({"access_token": "acc-dead", "refresh_token": "ref-old"}))
    new_pair = {"access_token": "acc-2", "refresh_token": "ref-2", "token_type": "Bearer"}
    s = _FakeSession({
        ("GET", "/conversations"): _Resp(401, {"error": "expired"}),
        ("POST", "/auth/refresh"): _Resp(200, new_pair),
    })

    token = _run(auth.get_access_token(
        "127.0.0.1:7836", cache_path=cache, env_file=env_file, session=s))

    assert token == "acc-2"
    assert s.calls == [("GET", "/conversations"), ("POST", "/auth/refresh")]
    assert json.loads(cache.read_text()) == {"access_token": "acc-2", "refresh_token": "ref-2"}
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_failed_refresh_falls_back_to_full_login(tmp_path, env_file):
    cache = tmp_path / "tui-token.json"
    cache.write_text(json.dumps({"access_token": "acc-dead", "refresh_token": "ref-dead"}))
    pair = {"access_token": "acc-3", "refresh_token": "ref-3", "token_type": "Bearer"}
    s = _FakeSession({
        ("GET", "/conversations"): _Resp(401, {"error": "expired"}),
        ("POST", "/auth/refresh"): _Resp(401, {"error": "Invalid or expired refresh token"}),
        ("POST", "/auth/login"): _Resp(200, pair),
    })

    token = _run(auth.get_access_token(
        "127.0.0.1:7836", cache_path=cache, env_file=env_file, session=s))

    assert token == "acc-3"
    assert s.calls == [
        ("GET", "/conversations"), ("POST", "/auth/refresh"), ("POST", "/auth/login"),
    ]


def test_login_posts_password_and_totp_from_env_file(tmp_path, env_file):
    cache = tmp_path / "tui-token.json"
    seen = {}

    def _login(kw):
        seen.update(kw["json"])
        return _Resp(200, {"access_token": "a", "refresh_token": "r", "token_type": "Bearer"})

    s = _FakeSession({("POST", "/auth/login"): _login})
    _run(auth.get_access_token(
        "127.0.0.1:7836", cache_path=cache, env_file=env_file, session=s))

    assert seen["password"] == "hunter2"
    assert len(seen["totp_code"]) == 6 and seen["totp_code"].isdigit()


def test_env_file_with_crlf_endings_parsed(tmp_path):
    p = tmp_path / "bobclaw.env"
    p.write_bytes(b"BOBCLAW_PASSWORD=hunter2\r\nTOTP_SECRET=JBSWY3DPEHPK3PXP\r\n")
    assert auth._load_credentials(p) == ("hunter2", "JBSWY3DPEHPK3PXP")


def test_no_mint_token_reference_remains():
    main = importlib.import_module("bobclaw_tui.__main__")
    assert not hasattr(main, "_mint_token")
    src = open(main.__file__, encoding="utf-8").read()
    assert "_mint_token" not in src
