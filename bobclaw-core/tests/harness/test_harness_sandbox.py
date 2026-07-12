import sys
import subprocess
import time
import pytest

from core.harness.sandbox import SubprocessSandbox, SandboxResult


def test_sandbox_ok():
    """A simple command succeeds and stdout contains 'ok'."""
    sandbox = SubprocessSandbox()
    result = sandbox.run([sys.executable, "-c", "print('ok')"])
    assert isinstance(result, SandboxResult)
    assert result.returncode == 0
    assert "ok" in result.stdout
    assert result.killed is False
    assert result.stderr == ""


def test_sandbox_nonzero_exit():
    """A command that exits with non-zero returncode gives returncode==3, not raised."""
    sandbox = SubprocessSandbox()
    result = sandbox.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert isinstance(result, SandboxResult)
    assert result.returncode == 3
    assert result.killed is False
    assert result.stdout == ""
    assert result.stderr == ""


def test_sandbox_timeout():
    """A timeout shorter than a sleep causes killed==True, not raised."""
    sandbox = SubprocessSandbox()
    start = time.monotonic()
    result = sandbox.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        timeout=0.5
    )
    elapsed = time.monotonic() - start
    assert isinstance(result, SandboxResult)
    assert result.killed is True
    # The process was terminated before it could produce output.
    assert result.returncode is not None  # On Unix, -15; on Windows maybe 1? But not 0.
    # Ensure we didn't wait anywhere near the full sleep. The bound must absorb
    # interpreter spawn + kill + collect under AV scanning (measured >2s per
    # spawn on some Windows boxes), so it is deliberately loose.
    assert elapsed < 15.0


def test_sandbox_timeout_preserves_partial_stderr():
    """Partial stderr produced before a timeout is returned, not the exception string.

    The timeout must comfortably exceed interpreter SPAWN latency, or the child
    is killed before it ever writes and this test fails falsely — measured
    1.2-2.2s per `python -c` spawn under AV scanning on Windows. 8s of timeout
    buys determinism at the cost of 8s of wall time.
    """
    sandbox = SubprocessSandbox()
    result = sandbox.run(
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stderr.write('partial-err\\n'); sys.stderr.flush(); time.sleep(60)",
        ],
        timeout=8,
    )
    assert isinstance(result, SandboxResult)
    assert result.killed is True
    assert "partial-err" in result.stderr
    # The exception repr must not replace the captured stderr bytes.
    assert "Command '" not in result.stderr
    assert "timed out" not in result.stderr.lower()


# ── audit r2 regressions: the "never raises" contract holds for bad output / bad executable ──

def test_sandbox_invalid_utf8_output_does_not_raise():
    """A worker emitting invalid UTF-8 must NOT raise UnicodeDecodeError out of run() (audit r2)."""
    sandbox = SubprocessSandbox()
    result = sandbox.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x00bad'); sys.stdout.flush()"]
    )
    assert isinstance(result, SandboxResult)
    assert result.returncode == 0
    assert result.killed is False  # replacement-decoded, never raised


def test_sandbox_missing_executable_does_not_raise():
    """A missing/invalid executable is reported in the result, not raised (audit r2)."""
    sandbox = SubprocessSandbox()
    result = sandbox.run(["this_binary_does_not_exist_ms6_xyz123", "--nope"])
    assert isinstance(result, SandboxResult)
    assert result.returncode != 0
    assert result.killed is False
    assert result.stderr  # the launch error is surfaced, not raised
