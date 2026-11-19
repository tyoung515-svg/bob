"""Hermetic OAuth rotation: fake lane files, frozen time, no real grants."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from core import lane_config as lc
from core.backends.openai_codex_auth import LaneCodexAuth, CodexProviderError, TOKEN_URL

NOW = 2000000000


def jwt(exp=NOW + 3600):
    payload = {"exp": exp, "https://api.openai.com/auth": {
        "chatgpt_account_id": "fake-account", "chatgpt_data_residency": "eu"}}
    return "fake." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".fake"


@pytest.fixture
def auth_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(lc.shutil, "which", lambda name: "/fake/codex")
    monkeypatch.setenv("BOB_LANE_ROOT", str(tmp_path))
    home = tmp_path / "codex-home"
    home.mkdir()
    path = home / "auth.json"
    path.write_text(json.dumps({"tokens": {"id_token": "fake-id-secret", "access_token": jwt(),
        "refresh_token": "fake-refresh-secret", "account_id": "fake-account", "future_token_field": 1},
        "last_refresh": "old", "OPENAI_API_KEY": "fake-key-secret", "future": {"keep": True}}))
    (tmp_path / "lane.env").write_text('BOB_LANE_DEFAULT_BACKEND="openai_codex"\n')
    lc._cached_lane.cache_clear()
    yield lc.read_lane(tmp_path), path
    lc._cached_lane.cache_clear()


def set_tokens(path, **changes):
    auth = json.loads(path.read_text())
    auth["tokens"].update(changes)
    path.write_text(json.dumps(auth))


@pytest.mark.asyncio
@pytest.mark.parametrize("exp,refreshes", [(NOW + 3600, 0), (NOW + 61, 0), (NOW + 59, 1), (NOW - 1, 1)])
async def test_refresh_only_when_expiring(auth_lane, exp, refreshes):
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(exp))
    calls = []
    def endpoint(request):
        assert str(request.url) == TOKEN_URL
        body = json.loads(request.content)
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "fake-refresh-secret"
        calls.append(request)
        return httpx.Response(200, json={"access_token": jwt(), "refresh_token": "fake-rotated-secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        result = await LaneCodexAuth(lane, clock=lambda: NOW).credentials(client)
    assert len(calls) == refreshes
    assert result.refresh == ("fake-rotated-secret" if refreshes else "fake-refresh-secret")


@pytest.mark.asyncio
async def test_atomic_save_preserves_shape_fields_and_mode(auth_lane, monkeypatch):
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(NOW - 1))
    old = json.loads(path.read_text())
    replaces = []
    real_replace = os.replace
    def replace(source, target):
        assert Path(source).parent == path.parent and target == path
        assert json.loads(Path(source).read_text())["tokens"]["refresh_token"] == "fake-next"
        replaces.append(source)
        real_replace(source, target)
    monkeypatch.setattr(os, "replace", replace)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "access_token": jwt(), "refresh_token": "fake-next", "id_token": "fake-id-next"}))) as client:
        await LaneCodexAuth(lane, clock=lambda: NOW).credentials(client)
    saved = json.loads(path.read_text())
    assert saved["future"] == old["future"] and saved["OPENAI_API_KEY"] == old["OPENAI_API_KEY"]
    assert saved["tokens"]["account_id"] == "fake-account"
    assert saved["tokens"]["future_token_field"] == 1
    assert saved["tokens"]["id_token"] == "fake-id-next"
    assert saved["last_refresh"] != "old" and replaces
    assert not list(path.parent.glob(".auth-bob-*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_peer_rotation_is_adopted_under_lock(auth_lane):
    lane, path = auth_lane
    auth = LaneCodexAuth(lane, clock=lambda: NOW)
    loaded = auth.load()
    set_tokens(path, access_token=jwt(NOW + 7200), refresh_token="fake-peer-next")
    def forbidden(request):
        pytest.fail("rotated token must be adopted without a refresh POST")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        adopted = await auth.credentials(client, previous=loaded)
    assert adopted.refresh == "fake-peer-next"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["refresh_token_reused", "invalid_grant"])
@pytest.mark.parametrize("peer", [True, False])
async def test_reuse_reread_adopts_or_requires_signin(auth_lane, code, peer, caplog):
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(NOW - 1))
    auth = LaneCodexAuth(lane, clock=lambda: NOW)
    calls = []
    def endpoint(request):
        calls.append(1)
        if peer:
            set_tokens(path, access_token=jwt(), refresh_token="fake-peer-secret")
        return httpx.Response(400, json={"error": {"code": code, "message": "fake-refresh-secret"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        if peer:
            assert (await auth.credentials(client)).refresh == "fake-peer-secret"
        else:
            with pytest.raises(CodexProviderError, match="sign in again") as error:
                await auth.credentials(client)
            assert "secret" not in str(error.value) + repr(error.value)
    assert calls == [1]
    assert "secret" not in caplog.text + repr(auth) + repr(auth.load())


@pytest.mark.asyncio
async def test_token_quota_and_transport_errors_are_redacted(auth_lane, caplog):
    caplog.set_level("DEBUG")
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(NOW - 1))
    original = path.read_bytes()
    for status in (429, 500):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="fake-refresh-secret"))) as client:
            with pytest.raises(CodexProviderError) as error:
                await LaneCodexAuth(lane, clock=lambda: NOW).credentials(client)
            assert error.value.status == status
            assert "sign in" not in str(error.value) and "secret" not in str(error.value)
    def fail(request):
        raise httpx.ConnectError("fake-refresh-secret", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(CodexProviderError) as error:
            await LaneCodexAuth(lane, clock=lambda: NOW).credentials(client)
        assert "secret" not in str(error.value) + repr(error.value)
    assert path.read_bytes() == original and "secret" not in caplog.text


@pytest.mark.asyncio
async def test_cross_process_lock_timeout_is_bounded(auth_lane):
    lane, path = auth_lane
    # Hold the actual advisory lock from a separate process, on either OS.
    code = '''import os, sys, time
f = open(sys.argv[1], "a+b")
if os.name == "nt":
 import msvcrt
 f.seek(0)
 msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
else:
 import fcntl
 fcntl.flock(f, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(path.with_name("auth.bob.lock"))],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5) == "locked\n"
        set_tokens(path, access_token=jwt(NOW - 1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no POST before lock"))) as client:
            with pytest.raises(CodexProviderError, match="lock timeout.*retry"):
                await asyncio.wait_for(LaneCodexAuth(lane, clock=lambda: NOW, lock_timeout=0.1).credentials(client), 2)
    finally:
        process.communicate("done\n", timeout=5)


def test_host_codex_home_env_and_api_key_only_ignored(tmp_path, monkeypatch):
    host = tmp_path / "host/.codex"
    host.mkdir(parents=True)
    (host / "auth.json").write_text(json.dumps({"tokens": {"access_token": "host-secret"}}))
    root = tmp_path / "lane"
    root.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(host))
    monkeypatch.setenv("OPENAI_API_KEY", "host-key-secret")
    monkeypatch.setattr(Path, "home", lambda: host.parent)
    lane = lc.read_lane(root)
    assert "openai_codex" not in lane.backends
    with pytest.raises(CodexProviderError, match="sign in again"):
        LaneCodexAuth(lane).load()
    home = root / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text('{"OPENAI_API_KEY":"fake-lane-key"}')
    assert "openai_codex" not in lc.read_lane(root).backends
    (root / "lane.env").write_text('CODEX_HOME=' + json.dumps(str(host)) + '\n')
    assert lc.read_lane(root).backends == {}


@pytest.mark.asyncio
async def test_concurrent_bob_refreshers_spend_token_once(auth_lane):
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(NOW - 1))
    calls = []
    async def endpoint(request):
        calls.append(1)
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"access_token": jwt(), "refresh_token": "fake-next-secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        first, second = await asyncio.gather(
            LaneCodexAuth(lane, clock=lambda: NOW).credentials(client),
            LaneCodexAuth(lane, clock=lambda: NOW).credentials(client))
    assert calls == [1] and first == second


@pytest.mark.asyncio
async def test_cli_rotation_during_successful_refresh_is_not_overwritten(auth_lane):
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(NOW - 1))
    def endpoint(request):
        set_tokens(path, access_token=jwt(NOW + 7200), refresh_token="fake-cli-next")
        return httpx.Response(200, json={"access_token": jwt(), "refresh_token": "fake-bob-next"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        adopted = await LaneCodexAuth(lane, clock=lambda: NOW).credentials(client)
    assert adopted.refresh == "fake-cli-next"
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "fake-cli-next"
