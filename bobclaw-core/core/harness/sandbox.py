from __future__ import annotations

import dataclasses
import subprocess
import typing


@dataclasses.dataclass
class SandboxResult:
    """Result of a sandboxed subprocess execution."""

    returncode: int
    stdout: str
    stderr: str
    killed: bool = False


class SubprocessSandbox:
    """Default sandbox that runs commands via subprocess.

    The production isolation sandbox is core.build.sandbox (Docker) and satisfies
    the same shape.  Non‑zero exit codes and timeouts are reported in the result,
    **never raised** – the caller's hands maps a dead process to a retryable error.
    """

    def run(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute *command* as a real subprocess and return a SandboxResult.

        Never uses ``shell=True``.  On timeout the process is killed (SIGKILL on
        POSIX, TerminateProcess on Windows) and the result has ``killed=True``;
        partial stdout/stderr (if any) are returned.  A normal exit, even non‑zero,
        is not considered a kill.  The method NEVER raises — invalid-UTF-8 output is
        replacement-decoded and a launch failure (missing/invalid executable) is
        reported in the result — so the caller's hands always maps a dead process to
        a retryable error (audit r2).
        """
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                # Invalid UTF-8 from the worker must NEVER raise UnicodeDecodeError out of the
                # sandbox (it would bypass the "never raises" contract) — replace bad bytes.
                errors="replace",
                timeout=timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            # POSIX quirk: TimeoutExpired carries BYTES even when run() was in
            # text mode (only the Windows kill-then-communicate path re-decodes),
            # so normalize here or the "never raises" contract leaks a bytes
            # stderr to callers expecting str.
            def _text(value: bytes | str | None) -> str:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value if value is not None else ""

            return SandboxResult(
                returncode=-1,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                killed=True,
            )
        except OSError as exc:
            # The executable is missing/invalid (FileNotFoundError / PermissionError, …) — the
            # worker never launched. Honor "never raises": report it as a FAILED (not killed)
            # result (127 = the conventional "command not found"); the caller's hands maps the
            # non-zero return to a retryable error.
            return SandboxResult(returncode=127, stdout="", stderr=str(exc), killed=False)

        return SandboxResult(
            returncode=proc.returncode,
            stdout=proc.stdout if proc.stdout is not None else "",
            stderr=proc.stderr if proc.stderr is not None else "",
            killed=False,
        )
