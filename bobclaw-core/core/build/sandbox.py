"""Build gates retain legacy Docker/subprocess behavior on unmarked hosts.

In packaged worker mode, auto/subprocess dispatch through the lane worker and
Docker is unavailable. Boundary failures never fall back to host execution.
"""
from __future__ import annotations

from core.worker import mode as execution_mode
from core.worker import client as worker_client

import logging
import subprocess
import uuid
from pathlib import Path

import core.config as _config
from core.build import skeleton

logger = logging.getLogger(__name__)

_MOUNT = "/work"
_PROBE_TIMEOUT = 15


class SandboxUnavailable(RuntimeError):
    """Raised when BUILD_SANDBOX='docker' is forced but the daemon/image is unavailable."""


def docker_ready() -> bool:
    """True iff the Docker daemon is reachable AND the sandbox image is present."""
    if execution_mode() == "legacy":
        try:
            info = subprocess.run(["docker", "info"], capture_output=True,
                                  timeout=_PROBE_TIMEOUT)
            if info.returncode != 0:
                return False
            img = subprocess.run(
                ["docker", "image", "inspect", _config.BUILD_SANDBOX_IMAGE],
                capture_output=True, timeout=_PROBE_TIMEOUT)
            return img.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        return False


def resolve_mode() -> str:
    """Resolve ``config.BUILD_SANDBOX`` to a concrete mode: ``docker`` | ``subprocess``.

    ``docker`` forced + unavailable → raise SandboxUnavailable (never silently fall
    back to un-isolated host execution). ``auto`` + unavailable → ``subprocess`` with a
    loud warning (the verify gate then runs LLM code un-isolated — static gate only).
    """
    mode = (_config.BUILD_SANDBOX or "auto").strip().lower()
    if execution_mode() == "legacy":
        if mode == "subprocess":
            return "subprocess"
        if mode == "docker":
            if docker_ready():
                return "docker"
            raise SandboxUnavailable(
                f"BUILD_SANDBOX=docker but the Docker daemon or image "
                f"{_config.BUILD_SANDBOX_IMAGE!r} is unavailable; build it with "
                f"`docker build -t {_config.BUILD_SANDBOX_IMAGE} -f docker/build-sandbox.Dockerfile docker`"
            )
        # auto
        if docker_ready():
            return "docker"
        logger.warning(
            "build sandbox: BUILD_SANDBOX=auto but Docker/image unavailable — the verify "
            "gate (LLM-written code) runs UN-ISOLATED on the host (static gate + env-strip "
            "only). Build %s and set BUILD_SANDBOX=docker for real isolation.",
            _config.BUILD_SANDBOX_IMAGE)
    else:
        if mode == "docker":
            raise SandboxUnavailable(worker_client.EXCLUDED_REASON)
        if mode not in {"auto", "subprocess", "worker"}:
            raise SandboxUnavailable("unavailable: unknown build execution mode")
    return "subprocess"


def _docker_argv(workspace: Path, inner: list[str], *, name: str) -> list[str]:
    """The hardened ``docker run`` argv. Defense in layers: ONLY the workspace is
    mounted (READ-ONLY — the gate never needs to write the app), ``--network none`` (no
    exfil), all Linux capabilities dropped, no-new-privileges, a read-only root fs with
    a small writable ``/tmp`` tmpfs, memory/pids/cpu caps, ``--rm`` (ephemeral), a
    ``--name`` so a timed-out run can be reaped, and a clean container env (no inherited
    host env at all). The host secrets/repo are never mounted, so the executing code
    cannot read them."""
    ws = str(Path(workspace).resolve())
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m",
        "--memory", _config.BUILD_SANDBOX_MEMORY,
        "--pids-limit", str(_config.BUILD_SANDBOX_PIDS),
        "--cpus", str(_config.BUILD_SANDBOX_CPUS),
        "-v", f"{ws}:{_MOUNT}:ro",
        "-w", _MOUNT,
        "-e", f"PYTHONPATH={_MOUNT}",
        "-e", "PYTHONIOENCODING=utf-8",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        _config.BUILD_SANDBOX_IMAGE,
        *inner,
    ]


def _run(workspace: Path, inner: list[str], timeout: int) -> subprocess.CompletedProcess:
    # Run a NAMED container so a timed-out run — which kills only the docker CLIENT, not
    # the container the daemon spawned — can be reaped; otherwise --rm never fires and
    # the (capped, isolated) container lingers, leaking on the fan-out/repair paths.
    name = f"bobclaw-build-{uuid.uuid4().hex[:12]}"
    try:
        if execution_mode() == "legacy":
            return subprocess.run(
                _docker_argv(workspace, inner, name=name),
                capture_output=True, text=True, timeout=timeout)
        else:
            return worker_client.refuse(
                _docker_argv(workspace, inner, name=name),
                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Best-effort reap (then --rm cleans up); never mask the original timeout.
        try:
            if execution_mode() == "legacy":
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
            else:
                worker_client.refuse(["docker", "kill", name], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        raise


def build_empty_ok(workspace: Path, *, timeout: int | None = None,
                   mode: str | None = None) -> bool:
    """Does the package import cleanly? (host or container per *mode*).

    ``mode`` lets the caller pin the resolution ONCE per verify pass (verify_node does)
    so build/test/CLI provably use the same mode and the daemon is probed once, not 3×.
    """
    timeout = timeout or _config.BUILD_VERIFY_TIMEOUT
    if (mode or resolve_mode()) == "subprocess":
        return skeleton.build_empty_ok(workspace)
    inner = ["python", "-c", f"import {skeleton.PACKAGE_NAME}"]
    return _run(workspace, inner, timeout).returncode == 0


def run_pytest(workspace: Path, *, timeout: int | None = None,
               mode: str | None = None) -> dict:
    """Run the suite (host or container per *mode*) → the shared pass/fail summary."""
    timeout = timeout or _config.BUILD_VERIFY_TIMEOUT
    if (mode or resolve_mode()) == "subprocess":
        return skeleton.run_pytest(workspace, timeout=timeout)
    inner = ["python", "-m", "pytest", "-q", "--no-header",
             "-p", "no:cacheprovider", "tests"]
    proc = _run(workspace, inner, timeout)
    return skeleton.parse_pytest_summary(proc.stdout + proc.stderr)


def run_cli(workspace: Path, *, timeout: int | None = None,
            mode: str | None = None) -> tuple[bool, str]:
    """Run the generated CLI (host or container per *mode*) → (ran_ok, output tail)."""
    timeout = timeout or _config.BUILD_VERIFY_TIMEOUT
    if (mode or resolve_mode()) == "subprocess":
        return skeleton.run_cli(workspace, timeout=timeout)
    inner = ["python", "-m", f"{skeleton.PACKAGE_NAME}.cli"]
    proc = _run(workspace, inner, timeout)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
