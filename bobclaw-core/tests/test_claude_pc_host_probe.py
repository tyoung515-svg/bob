"""Windows SSH uses cmd.exe, not POSIX true (2026-10-24 regression)."""
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


@pytest.fixture(autouse=True)
def isolate_host_cache(monkeypatch):
    monkeypatch.setattr(pc, '_HOST_CACHE', {'host': None, 'expires': 0.0})
    monkeypatch.delenv('BOB_CLAUDE_HOST', raising=False)

@pytest.mark.asyncio
async def test_windows_host_with_cmd_and_no_posix_true_is_up(monkeypatch):
    async def spawn(*argv, **kwargs):
        class Process:
            async def wait(self):
                return 0 if argv[-1] == 'cmd /c exit 0' else 1
        return Process()
    monkeypatch.setattr(pc.asyncio, 'create_subprocess_exec', spawn)
    assert await pc._host_up(pc.WINDOWS_HOST) is True

@pytest.mark.asyncio
async def test_linux_host_keeps_posix_probe(monkeypatch):
    async def spawn(*argv, **kwargs):
        class Process:
            async def wait(self):
                return 0 if argv[-1] == 'true' else 1
        return Process()
    monkeypatch.setattr(pc.asyncio, 'create_subprocess_exec', spawn)
    assert await pc._host_up(pc.LINUX_HOST) is True

@pytest.mark.asyncio
async def test_auto_falls_back_to_windows_without_posix_tools(monkeypatch):
    async def spawn(*argv, **kwargs):
        host, command = argv[-2:]
        class Process:
            async def wait(self):
                return 0 if host == pc.WINDOWS_HOST and command == 'cmd /c exit 0' else 1
        return Process()
    monkeypatch.setattr(pc.asyncio, 'create_subprocess_exec', spawn)
    assert await pc.choose_host() == pc.WINDOWS_HOST

@pytest.mark.asyncio
async def test_both_hosts_unavailable_still_fails_closed(monkeypatch):
    async def spawn(*argv, **kwargs):
        class Process:
            async def wait(self):
                return 1
        return Process()
    monkeypatch.setattr(pc.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(pc.ClaudePCError, match='claude_pc_unreachable'):
        await pc.choose_host()


@pytest.mark.asyncio
async def test_no_configured_hosts_never_probes(monkeypatch):
    monkeypatch.setattr(pc, "LINUX_HOST", "")
    monkeypatch.setattr(pc, "WINDOWS_HOST", "")
    async def forbidden(*args, **kwargs):
        raise AssertionError("no host may be probed")
    monkeypatch.setattr(pc.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(pc.ClaudePCError, match="configure a Linux or Windows host"):
        await pc.choose_host()
