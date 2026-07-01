"""
BoBClaw Claude Build Pipeline — Tool Definitions & Executor

Defines the Claude tool schemas and a sandboxed executor that restricts all
file operations to /tmp/bobclaw-builds/{session_id}/ and only allows a
whitelist of shell commands.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Optional

from config import BUILD_WORKSPACE_ROOT, BUILD_TIMEOUT_SECONDS

# ---------------------------------------------------------------------------
# Tool schemas (Claude /messages API format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "code_execute",
        "description": (
            "Execute a snippet of code inside the sandboxed build workspace. "
            "Supported interpreters: python3, node, bash."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpreter": {
                    "type": "string",
                    "enum": ["python3", "node", "bash"],
                    "description": "Language interpreter to use.",
                },
                "code": {
                    "type": "string",
                    "description": "Source code to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before execution is killed (default 30).",
                    "default": 30,
                },
            },
            "required": ["interpreter", "code"],
        },
    },
    {
        "name": "file_read",
        "description": "Read the contents of a file within the build workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path inside the build workspace.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": "Write content to a file within the build workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path inside the build workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "shell_run",
        "description": (
            "Run a restricted shell command inside the build workspace. "
            "Only a safe whitelist of commands is permitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the command is killed (default 60).",
                    "default": 60,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "test_run",
        "description": "Run the test suite (pytest or npm test) inside the build workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "framework": {
                    "type": "string",
                    "enum": ["pytest", "npm"],
                    "description": "Test framework to invoke.",
                },
                "args": {
                    "type": "string",
                    "description": "Extra arguments forwarded to the test runner.",
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the runner is killed (default 120).",
                    "default": 120,
                },
            },
            "required": ["framework"],
        },
    },
]

# ---------------------------------------------------------------------------
# Shell command argv-based whitelist
# ---------------------------------------------------------------------------
#
# Historical note: the previous implementation used regex match/deny pairs
# against the *raw* command string, then handed the string to
# ``asyncio.create_subprocess_shell``.  That launched ``/bin/sh -c <cmd>``,
# which meant any regex bypass (word-boundary tricks, unicode lookalikes,
# nested quoting) translated directly into shell-interpreted execution.
#
# The hardened model (B2) is:
#
#   1. Tokenise with ``shlex.split`` — no shell.
#   2. Reject if argv is empty, if argv[0] contains a path separator, or
#      if argv[0] is not in a small whitelist of build-time binaries.
#   3. Reject any arg that is itself a shell operator (``|``, ``&&``, ...)
#      or contains command-substitution markers (`` ` ``, ``$(``).
#   4. Reject any arg containing a ``..`` path component.
#   5. Run the resulting argv via ``asyncio.create_subprocess_exec`` —
#      never via a shell.
#
# Under this model, even if step 3 or 4 misses something, step 5 means the
# operator-as-literal-arg is harmless (e.g. ``echo`` prints ``|`` to stdout
# rather than piping to another process).  The validation exists primarily
# to return clear errors and to keep the behaviour observable.

_ARGV_WHITELIST: frozenset[str] = frozenset(
    {
        # core utils
        "ls", "pwd", "echo", "cat", "mkdir", "cp", "mv", "touch",
        "find", "grep", "head", "tail", "wc", "diff",
        # python
        "pip", "pip3", "python", "python3", "pytest",
        # node / js
        "node", "npm", "npx",
        # compiled / build tooling
        "make", "cargo", "go", "java", "javac", "mvn", "gradle",
    }
)

# Tokens that only make sense inside a real shell.  Passed to
# ``subprocess_exec`` they're harmless literals — we reject them anyway so
# callers get a clear error rather than silently broken behaviour.
_FORBIDDEN_ARG_TOKENS: frozenset[str] = frozenset(
    {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "`"}
)


def _parse_and_validate_command(
    command: str,
) -> tuple[Optional[list[str]], Optional[str]]:
    """Parse *command* into argv and validate it against the policy.

    Returns ``(argv, None)`` when the command is permitted, or
    ``(None, reason)`` when rejected.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, f"invalid shell syntax: {exc}"

    if not argv:
        return None, "empty command"

    head = argv[0]
    if "/" in head or "\\" in head:
        return None, f"argv[0] must be a bare binary name, not a path: {head!r}"
    if head.startswith("-"):
        return None, f"argv[0] must not start with a flag: {head!r}"
    if head not in _ARGV_WHITELIST:
        return None, f"binary not in whitelist: {head!r}"

    for arg in argv[1:]:
        if arg in _FORBIDDEN_ARG_TOKENS:
            return None, f"forbidden shell operator in args: {arg!r}"
        if "`" in arg or "$(" in arg:
            return None, f"command substitution not allowed: {arg!r}"
        # Path traversal — catches "../foo", "foo/../bar", "..\\foo".
        parts = arg.replace("\\", "/").split("/")
        if ".." in parts:
            return None, f"path traversal not allowed: {arg!r}"

    return argv, None


def _is_command_allowed(command: str) -> bool:
    """Back-compat boolean wrapper around :func:`_parse_and_validate_command`."""
    argv, _reason = _parse_and_validate_command(command)
    return argv is not None


# ---------------------------------------------------------------------------
# Sandbox path enforcement
# ---------------------------------------------------------------------------

class SandboxViolationError(ValueError):
    """Raised when a path escapes the sandbox workspace."""


def _resolve_sandbox_path(workspace: Path, relative_path: str) -> Path:
    """
    Resolve *relative_path* inside *workspace* and verify it stays within the
    workspace.  Raises SandboxViolationError on path traversal attempts.
    """
    # Reject absolute paths outright
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise SandboxViolationError(
            f"Absolute paths are not allowed: '{relative_path}'"
        )

    resolved = (workspace / candidate).resolve()
    workspace_resolved = workspace.resolve()

    try:
        resolved.relative_to(workspace_resolved)
    except ValueError:
        raise SandboxViolationError(
            f"Path '{relative_path}' escapes the build workspace."
        )

    return resolved


# ---------------------------------------------------------------------------
# Tool Executor
# ---------------------------------------------------------------------------

class ToolExecutor:
    """
    Executes Claude tool calls inside a sandboxed build workspace.

    Parameters
    ----------
    session_id:
        Used to derive the workspace path:
        BUILD_WORKSPACE_ROOT / session_id
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.workspace: Path = BUILD_WORKSPACE_ROOT / session_id
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def execute(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Dispatch a tool call and return a result dict suitable for inclusion
        in a Claude tool_result content block.
        """
        handlers = {
            "code_execute": self._code_execute,
            "file_read": self._file_read,
            "file_write": self._file_write,
            "shell_run": self._shell_run,
            "test_run": self._test_run,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: '{tool_name}'"}

        try:
            return await handler(tool_input)
        except SandboxViolationError as exc:
            return {"error": f"Sandbox violation: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Tool execution error: {exc}"}

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _code_execute(self, inp: dict[str, Any]) -> dict[str, Any]:
        interpreter = inp["interpreter"]
        code = inp["code"]
        timeout = int(inp.get("timeout", 30))

        # Validate interpreter
        allowed_interpreters = {"python3", "node", "bash"}
        if interpreter not in allowed_interpreters:
            return {"error": f"Interpreter '{interpreter}' is not allowed."}

        if timeout > BUILD_TIMEOUT_SECONDS:
            timeout = BUILD_TIMEOUT_SECONDS

        try:
            proc = await asyncio.create_subprocess_exec(
                interpreter,
                "-c" if interpreter in ("python3", "bash") else "--eval",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "returncode": proc.returncode,
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"Code execution timed out after {timeout}s"}

    async def _file_read(self, inp: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_sandbox_path(self.workspace, inp["path"])
        if not path.exists():
            return {"error": f"File not found: '{inp['path']}'"}
        if not path.is_file():
            return {"error": f"Path is not a file: '{inp['path']}'"}
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"content": content}

    async def _file_write(self, inp: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_sandbox_path(self.workspace, inp["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"], encoding="utf-8")
        return {"written": str(path.relative_to(self.workspace)), "bytes": len(inp["content"])}

    async def _shell_run(self, inp: dict[str, Any]) -> dict[str, Any]:
        command = inp["command"]
        timeout = int(inp.get("timeout", 60))

        argv, reason = _parse_and_validate_command(command)
        if argv is None:
            return {
                "error": (
                    f"Command rejected by security policy ({reason}): "
                    f"'{command}'. Only a safe whitelist of binaries is "
                    "permitted and shell operators are disallowed."
                )
            }

        if timeout > BUILD_TIMEOUT_SECONDS:
            timeout = BUILD_TIMEOUT_SECONDS

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "returncode": proc.returncode,
            }
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
            return {"error": f"Shell command timed out after {timeout}s"}
        except FileNotFoundError:
            return {"error": f"Binary not found on PATH: '{argv[0]}'"}

    async def _test_run(self, inp: dict[str, Any]) -> dict[str, Any]:
        framework = inp["framework"]
        extra_args_raw = inp.get("args", "") or ""
        timeout = int(inp.get("timeout", 120))

        try:
            extra_argv = shlex.split(extra_args_raw) if extra_args_raw else []
        except ValueError as exc:
            return {"error": f"Invalid test args (shell syntax): {exc}"}

        # Apply the same arg-level hardening as _shell_run.  argv[0] is
        # fixed by us, so we only need to vet the extras.
        for arg in extra_argv:
            if arg in _FORBIDDEN_ARG_TOKENS:
                return {
                    "error": f"Forbidden shell operator in args: '{arg}'"
                }
            if "`" in arg or "$(" in arg:
                return {
                    "error": f"Command substitution not allowed in args: '{arg}'"
                }
            parts = arg.replace("\\", "/").split("/")
            if ".." in parts:
                return {"error": f"Path traversal not allowed in args: '{arg}'"}

        if framework == "pytest":
            argv = ["pytest", *extra_argv]
        elif framework == "npm":
            argv = ["npm", "test", *extra_argv]
        else:
            return {"error": f"Unknown test framework: '{framework}'"}

        if timeout > BUILD_TIMEOUT_SECONDS:
            timeout = BUILD_TIMEOUT_SECONDS

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "returncode": proc.returncode,
            }
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
            return {"error": f"Test run timed out after {timeout}s"}
        except FileNotFoundError:
            return {"error": f"Test runner not found on PATH: '{argv[0]}'"}

    # ------------------------------------------------------------------
    # Artifact harvesting
    # ------------------------------------------------------------------

    def collect_artifacts(self) -> list[dict[str, str]]:
        """Return all files created in the workspace as artifact descriptors."""
        artifacts = []
        for fpath in sorted(self.workspace.rglob("*")):
            if fpath.is_file():
                rel = str(fpath.relative_to(self.workspace))
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001 — binary/unreadable file
                    content = "<binary>"
                artifacts.append({"path": rel, "content": content})
        return artifacts
