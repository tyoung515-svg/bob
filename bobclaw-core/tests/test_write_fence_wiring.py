from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_WIN = REPO_ROOT / "scripts" / "win"
LAUNCHER_INVENTORY = {
    "docker-compose.yml",
    "install-bob.ps1",
    "scripts/win/install-durability.ps1",
    "scripts/win/start-all.ps1",
    "scripts/win/start-core.ps1",
    "scripts/win/start-local.ps1",
    "scripts/win/task-core.ps1",
}


def _executable_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _memory_capable_core_launchers() -> set[str]:
    """Inventory paths that can launch a core which may have memory enabled."""
    found = {"docker-compose.yml"}
    launcher_paths = [*REPO_ROOT.glob("*.ps1"), *SCRIPTS_WIN.glob("*.ps1")]
    for path in launcher_paths:
        executable = _executable_lines(path.read_text(encoding="utf-8"))
        launches_core = (
            re.search(r"(?m)^\s*& \$py start\.py", executable) is not None
            or "Spawn-Service 'bobclaw-core'" in executable
            or (
                "Register-Wrapper 'BobClaw-Core'" in executable
                and "task-core.ps1" in executable
            )
            or "Start-ScheduledTask -TaskName 'BobClaw-Core'" in executable
            or "scripts\\win\\start-local.ps1" in executable
        )
        if launches_core:
            found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_memory_capable_core_launcher_inventory_is_complete():
    """New core launch paths must be classified by the bootstrap invariant."""
    assert _memory_capable_core_launchers() == LAUNCHER_INVENTORY


def test_shipped_install_path_reaches_start_local_without_forcing_memory_flags():
    installer = (REPO_ROOT / "install-bob.ps1").read_text(encoding="utf-8")
    start_local = (SCRIPTS_WIN / "start-local.ps1").read_text(encoding="utf-8")
    assert "scripts\\win\\start-local.ps1" in installer
    assert "Spawn-Service 'bobclaw-core'" in start_local
    assert not re.search(r"(?m)^\s*\$env:MEMORY_ENABLED\s*=", start_local)
    assert not re.search(r"(?m)^\s*\$env:MEMORY_WRITE_FENCE_ENABLED\s*=", start_local)


def test_env_example_documents_fence_next_to_memory_enablement():
    text = (REPO_ROOT / ".secrets" / "bobclaw.env.example").read_text(
        encoding="utf-8"
    )
    memory_index = text.index("MEMORY_ENABLED=false")
    fence_index = text.index("MEMORY_WRITE_FENCE_ENABLED")
    assert memory_index < fence_index < text.index("MEMORY_QDRANT_URL")


def test_requirements_lock_contains_installed_filelock_pin():
    lock = (REPO_ROOT / "bobclaw-core" / "requirements.lock").read_text(
        encoding="utf-8"
    )
    assert re.search(r"(?m)^filelock==3\.29\.5$", lock)
