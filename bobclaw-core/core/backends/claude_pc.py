"""Read-only Claude faces through a PC's subscription CLI, never server auth.

The Claude route uses explicitly configured Linux and Windows SSH hosts.
Host selection probes Linux first, then Windows, and briefly caches the winner.
An alternate account is pinned separately and never joins that probe.

The server never holds Claude credentials; the CLI on the PC owns the login.
Whatever the host, the reply must carry a ``modelUsage`` key equal to the seat's
exact model, or the turn fails closed.
"""

from core.worker import mode as execution_mode
from core.worker import client as worker_client
import asyncio
import json
import os
import time

class ClaudePCError(RuntimeError): pass

# Liveness cache: (host_name, expires_at). A probe costs one BatchMode ssh.
_HOST_CACHE: dict[str, object] = {"host": None, "expires": 0.0}
HOST_PROBE_TTL = 60.0
LINUX_HOST = os.environ.get("BOB_CLAUDE_LINUX_HOST", "")
WINDOWS_HOST = os.environ.get("BOB_CLAUDE_WINDOWS_HOST", "")
# The alternate account never joins the primary liveness probe.
# A missing primary host must never consume the alternate account quota.
ALTERNATE_HOST = os.environ.get("BOB_CLAUDE_ALTERNATE_HOST", "")
_WINDOWS_HOSTS = (WINDOWS_HOST, ALTERNATE_HOST)


async def _host_up(host: str) -> bool:
    if not host:
        return False
    try:
        # Windows SSH lands in cmd.exe, which does not provide POSIX `true`.
        probe = "cmd /c exit 0" if host in _WINDOWS_HOSTS else "true"
        if execution_mode() == "legacy":
            process = await asyncio.create_subprocess_exec(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, probe,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        else:
            process = await worker_client.refuse_async(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, probe,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return await asyncio.wait_for(process.wait(), timeout=8) == 0
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        return False


async def choose_host() -> str:
    """Return the PC host that will serve this turn.

    BOB_CLAUDE_HOST may pin Windows or Linux; auto probes configured hosts.
    """
    pinned = (os.getenv("BOB_CLAUDE_HOST") or "auto").strip().lower()
    if pinned in ("win", "windows") or (WINDOWS_HOST and pinned == WINDOWS_HOST.lower()):
        if not WINDOWS_HOST:
            raise ClaudePCError("Claude PC is unavailable: set BOB_CLAUDE_WINDOWS_HOST.")
        return WINDOWS_HOST
    if pinned == "linux" or (LINUX_HOST and pinned == LINUX_HOST.lower()):
        if not LINUX_HOST:
            raise ClaudePCError("Claude PC is unavailable: set BOB_CLAUDE_LINUX_HOST.")
        return LINUX_HOST
    if not (LINUX_HOST or WINDOWS_HOST):
        raise ClaudePCError("Claude PC is unavailable: configure a Linux or Windows host.")

    now = time.monotonic()
    cached = _HOST_CACHE.get("host")
    if cached and float(_HOST_CACHE.get("expires") or 0) > now:
        return str(cached)
    for host in (LINUX_HOST, WINDOWS_HOST):
        if host and await _host_up(host):
            _HOST_CACHE["host"] = host
            _HOST_CACHE["expires"] = now + HOST_PROBE_TTL
            return host
    raise ClaudePCError("claude_pc_unreachable")


class ClaudePCClient:
    # Exact model the PC helper must report in modelUsage (fail-closed on
    # anything else) and the helper scripts that pin it CLI-side.
    MODEL = 'claude-opus-5-5'
    WIN_SCRIPT = ""
    WIN_SCRIPT_ENV = "BOB_CLAUDE_WINDOWS_SCRIPT"
    # Linux helper takes the model slug as argv[1]; Windows ps1 is model-pinned.
    LINUX_SCRIPT = ""
    LINUX_SCRIPT_ENV = "BOB_CLAUDE_LINUX_SCRIPT"
    # Empty ⇒ resolve through choose_host() (the primary account). A subclass pins HOST to
    # serve a seat on a different account, and never enters the shared probe.
    HOST = ''
    HOST_SETTING = ''
    # True ⇒ the Windows helper takes the model slug as argv[1] (the alternate account contract).
    WIN_SCRIPT_ARG = False
    # Reasoning effort for CLIs that accept --effort (Claude). Empty ⇒ flag omitted,
    # which keeps every existing seat byte-identical.
    EFFORT = ""
    VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
    # Manager-tier turns on a large mounted evidence base run ~200–430s on the
    # subscription CLI (measured 2026-10-16: 199.9s api for a 30KB prompt;
    # 380s for 53KB; 74KB reconciliation exceeded 420s). 900 bounds a stalled
    # call without killing real work; manager dispatches are supervised and
    # serial, so a long ceiling is acceptable.
    TIMEOUT = 900

    @classmethod
    def parse(cls, raw, returncode):
        if returncode: raise ClaudePCError('claude_pc_cli_failed')
        try: data = json.loads(raw)
        except (ValueError, TypeError): raise ClaudePCError('claude_pc_invalid_response')
        if data.get('is_error') or data.get('subtype') != 'success': raise ClaudePCError('claude_pc_generation_failed')
        models = data.get('modelUsage') or {}
        if set(models) != {cls.MODEL}: raise ClaudePCError('claude_pc_model_mismatch')
        text = data.get('result')
        if not isinstance(text, str) or not text.strip(): raise ClaudePCError('claude_pc_empty_response')
        return text

    def _win_invocation(self) -> str:
        """The remote PowerShell command for a Windows host.

        Primary ps1 files are model-PINNED, so no argument is appended. The alternate helper
        takes ``<model> [effort]`` (the same shape as claude-face.sh), and effort is
        validated against a fixed set so a seat row can never inject shell text.
        """
        script = os.environ.get(self.WIN_SCRIPT_ENV) or self.WIN_SCRIPT
        if not script or not script.strip():
            raise ClaudePCError("Claude PC is unavailable: set " + self.WIN_SCRIPT_ENV + ".")
        remote = 'powershell -NoProfile -ExecutionPolicy Bypass -File ' + script
        if not self.WIN_SCRIPT_ARG:
            return remote
        remote += ' ' + self.MODEL
        if self.EFFORT:
            if self.EFFORT not in self.VALID_EFFORTS:
                raise ClaudePCError('invalid_effort')
            remote += ' ' + self.EFFORT
        return remote

    def _argv(self, host: str) -> list[str]:
        if host in _WINDOWS_HOSTS:
            return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
                    self._win_invocation()]
        script = os.environ.get(self.LINUX_SCRIPT_ENV) or self.LINUX_SCRIPT
        if not script or not script.strip():
            raise ClaudePCError("Claude PC is unavailable: set " + self.LINUX_SCRIPT_ENV + ".")
        return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                host, 'bash', script, self.MODEL]

    def _timeout(self) -> float:
        """Wall-clock ceiling for one seat call.

        high/xhigh reasoning on a large prompt runs past a base ceiling tuned for a
        short turn (measured 2026-10-31: a 5.3 KB review at xhigh was still running
        past 420 s), so effort buys proportionally more time rather than a fixed bump.
        """
        return self.TIMEOUT * 1.5 if self.EFFORT in ("high", "xhigh") else self.TIMEOUT

    async def chat(self, prompt, host: str | None = None):
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 200000: raise ClaudePCError('invalid_prompt')
        if self.HOST_SETTING and not self.HOST:
            raise ClaudePCError("Claude PC is unavailable: set " + self.HOST_SETTING + ".")
        host = host or self.HOST or await choose_host()
        if execution_mode() == "legacy":
            process = await asyncio.create_subprocess_exec(
                *self._argv(host),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        else:
            process = await worker_client.refuse_async(
                *self._argv(host),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(process.communicate(prompt.encode('utf-8')), timeout=self._timeout())
        except (TimeoutError, asyncio.CancelledError):
            process.kill(); await process.wait(); raise
        return self.parse(out.decode('utf-8-sig'), process.returncode)


class ClaudePCSonnetClient(ClaudePCClient):
    MODEL = 'claude-sonnet-5-5'
    WIN_SCRIPT_ENV = "BOB_CLAUDE_SONNET_WINDOWS_SCRIPT"


class ClaudePCHaikuClient(ClaudePCClient):
    # Cheap worker seat. Same strict route; the Linux helper takes the slug, and
    # a Windows ps1 would need the same --model pin before this is used there.
    MODEL = 'claude-haiku-5-5'
    WIN_SCRIPT_ENV = "BOB_CLAUDE_HAIKU_WINDOWS_SCRIPT"


# Alternate-account seats remain opt-in and separately pinned.
# The alternate account never draws on the primary subscription.
ALTERNATE_SCRIPT = ""


class ClaudeAlternateHaikuClient(ClaudePCClient):
    HOST = ALTERNATE_HOST
    MODEL = 'claude-haiku-5-5'
    HOST_SETTING = "BOB_CLAUDE_ALTERNATE_HOST"
    WIN_SCRIPT = ALTERNATE_SCRIPT
    WIN_SCRIPT_ENV = "BOB_CLAUDE_ALTERNATE_WINDOWS_SCRIPT"
    WIN_SCRIPT_ARG = True
    # One heavy spawn on a Pro plan; a stalled call is bounded well below the
    # primary-seat 900s because these are worker-shaped turns, not evidence managers.
    # high/xhigh multiply this (see _timeout).
    TIMEOUT = 480


class ClaudeAlternateSonnetClient(ClaudeAlternateHaikuClient):
    MODEL = 'claude-sonnet-5-5'


class ClaudeAlternateOpusClient(ClaudeAlternateHaikuClient):
    # The most quota-hungry seat reachable here (~30x a haiku turn on the same prompt),
    # so it gets a longer ceiling and is never a default or an escalation target.
    MODEL = 'claude-opus-5-5'
    TIMEOUT = 600
