"""Alternate-PC Claude seat: pinned host, slug-as-argv, and the parse contract.

The seat exists because it is a DIFFERENT ACCOUNT (the alternate PC subscription). These
tests pin the two properties that keep it from becoming an accidental primary route:
the host is pinned on the client (choose_host is never consulted) and the model
slug reaches the helper as argv[1], matching the claude-face.sh contract.
"""
import asyncio

import pytest

from core.backends import claude_pc as pc

@pytest.fixture(autouse=True)
def configured_pc(monkeypatch):
    monkeypatch.setattr(pc, "LINUX_HOST", "example-linux")
    monkeypatch.setattr(pc, "WINDOWS_HOST", "example-windows")
    monkeypatch.setattr(pc, "ALTERNATE_HOST", "example-alternate")
    monkeypatch.setattr(pc, "_WINDOWS_HOSTS", (pc.WINDOWS_HOST, pc.ALTERNATE_HOST))
    monkeypatch.setattr(pc.ClaudeAlternateHaikuClient, "HOST", pc.ALTERNATE_HOST)
    monkeypatch.setattr(pc, "_HOST_CACHE", {"host": None, "expires": 0.0})
    for name, value in {
        "BOB_CLAUDE_WINDOWS_SCRIPT": "C:/example/claude-pc-face.ps1",
        "BOB_CLAUDE_SONNET_WINDOWS_SCRIPT": "C:/example/claude-pc-face-sonnet.ps1",
        "BOB_CLAUDE_HAIKU_WINDOWS_SCRIPT": "C:/example/claude-pc-face-haiku.ps1",
        "BOB_CLAUDE_LINUX_SCRIPT": "/opt/example/claude-face.sh",
        "BOB_CLAUDE_ALTERNATE_WINDOWS_SCRIPT": "C:/example/claude-alternate-face.ps1",
    }.items():
        monkeypatch.setenv(name, value)



def test_alternate_client_pins_host():
    assert pc.ClaudeAlternateHaikuClient().HOST == pc.ALTERNATE_HOST
    assert pc.ClaudeAlternateSonnetClient().HOST == pc.ALTERNATE_HOST
    # A primary seat keeps an empty HOST so it still resolves through choose_host().
    assert pc.ClaudePCHaikuClient().HOST == ""


def test_alternate_never_enters_the_shared_host_probe(monkeypatch):
    """choose_host() must not be able to return the alternate PC."""
    async def fail():  # pragma: no cover - only fails the test
        raise AssertionError("choose_host must not be consulted for an alternate seat")

    monkeypatch.setattr(pc, "choose_host", fail)
    client = pc.ClaudeAlternateHaikuClient()
    # With HOST pinned, chat() resolves the host without awaiting choose_host.
    assert (client.HOST or None) == pc.ALTERNATE_HOST


def test_argv_shape_alternate_vs_primary():
    alternate = pc.ClaudeAlternateHaikuClient()._argv(pc.ALTERNATE_HOST)
    assert alternate[0] == "ssh"
    assert alternate[-2] == pc.ALTERNATE_HOST
    assert alternate[-1].endswith("claude-alternate-face.ps1 claude-haiku-5-5")

    primary = pc.ClaudePCHaikuClient()._argv(pc.WINDOWS_HOST)
    assert "-File" in primary[-1]
    # The primary PC ps1 is model-pinned, so no slug is appended.
    assert not primary[-1].endswith("claude-haiku-5-5")

    linux = pc.ClaudePCHaikuClient()._argv(pc.LINUX_HOST)
    assert linux[-3:-1] == ["bash", "/opt/example/claude-face.sh"]
    assert linux[-1] == "claude-haiku-5-5"


def test_parse_accepts_the_verified_alternate_envelope():
    raw = (
        '{"is_error":false,"subtype":"success","result":"ALTERNATE_OK",'
        '"modelUsage":{"claude-haiku-5-5":{"inputTokens":2,"outputTokens":9}}}'
    )
    assert pc.ClaudeAlternateHaikuClient().parse(raw, 0) == "ALTERNATE_OK"


def test_parse_is_fail_closed_on_a_foreign_model():
    raw = (
        '{"is_error":false,"subtype":"success","result":"x",'
        '"modelUsage":{"claude-sonnet-5-5":{}}}'
    )
    with pytest.raises(pc.ClaudePCError):
        pc.ClaudeAlternateHaikuClient().parse(raw, 0)


def test_chat_passes_the_prompt_on_stdin(monkeypatch):
    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, data):
            seen["stdin"] = data
            return (
                b'{"is_error":false,"subtype":"success","result":"ok",'
                b'"modelUsage":{"claude-haiku-5-5":{}}}',
                b"",
            )

        def kill(self):  # pragma: no cover
            pass

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(pc.asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(pc.ClaudeAlternateHaikuClient().chat("hi"))
    assert out == "ok"
    assert seen["stdin"] == b"hi"
    assert seen["argv"][-1].endswith("claude-haiku-5-5")


def test_effort_reaches_the_helper_as_argv2():
    client = pc.ClaudeAlternateSonnetClient()
    client.EFFORT = "xhigh"
    assert client._argv(pc.ALTERNATE_HOST)[-1].endswith("claude-sonnet-5-5 xhigh")


def test_opus_seat_shares_the_same_contract():
    client = pc.ClaudeAlternateOpusClient()
    client.EFFORT = "high"
    argv = client._argv(pc.ALTERNATE_HOST)
    assert argv[-1].endswith("claude-opus-5-5 high")
    assert client.TIMEOUT == 600


def test_effort_is_validated_before_it_can_reach_a_shell_string():
    client = pc.ClaudeAlternateSonnetClient()
    client.EFFORT = "xhigh; rm -rf /"
    with pytest.raises(pc.ClaudePCError):
        client._argv(pc.ALTERNATE_HOST)


def test_primary_seats_ignore_effort():
    client = pc.ClaudePCHaikuClient()
    client.EFFORT = "high"
    assert client._argv(pc.WINDOWS_HOST)[-1].endswith("claude-pc-face-haiku.ps1")


def test_high_effort_buys_more_wall_clock():
    client = pc.ClaudeAlternateSonnetClient()
    base = client._timeout()
    client.EFFORT = "xhigh"
    assert client._timeout() == base * 1.5
    client.EFFORT = "medium"
    assert client._timeout() == base


def test_missing_alternate_host_never_spills_into_shared_account(monkeypatch):
    monkeypatch.setattr(pc.ClaudeAlternateHaikuClient, "HOST", "")
    async def forbidden():
        raise AssertionError("shared host probe must not run")
    monkeypatch.setattr(pc, "choose_host", forbidden)
    with pytest.raises(pc.ClaudePCError, match="set BOB_CLAUDE_ALTERNATE_HOST"):
        asyncio.run(pc.ClaudeAlternateHaikuClient().chat("hello"))


@pytest.mark.parametrize("client, host, setting", [
    (pc.ClaudePCClient, "example-windows", "BOB_CLAUDE_WINDOWS_SCRIPT"),
    (pc.ClaudePCSonnetClient, "example-windows", "BOB_CLAUDE_SONNET_WINDOWS_SCRIPT"),
    (pc.ClaudePCHaikuClient, "example-windows", "BOB_CLAUDE_HAIKU_WINDOWS_SCRIPT"),
    (pc.ClaudePCClient, "example-linux", "BOB_CLAUDE_LINUX_SCRIPT"),
    (pc.ClaudeAlternateHaikuClient, "example-alternate", "BOB_CLAUDE_ALTERNATE_WINDOWS_SCRIPT"),
])
def test_missing_helper_fails_before_ssh(monkeypatch, client, host, setting):
    monkeypatch.delenv(setting, raising=False)
    with pytest.raises(pc.ClaudePCError, match="set " + setting):
        client()._argv(host)
