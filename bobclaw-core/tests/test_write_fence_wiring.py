from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_WIN = REPO_ROOT / "scripts" / "win"
LAUNCHER_INVENTORY = {
    "Makefile",
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


def _powershell_text_launches_core(text: str) -> bool:
    executable = _executable_lines(text)
    return (
        re.search(
            r"(?im)^\s*(?:&\s+)?(?:\$py|python(?:\.exe)?)\s+start\.py(?:\s|$)",
            executable,
        )
        is not None
        or re.search(r"(?im)^\s*&\s+\.\\start\.py(?:\s|$)", executable)
        is not None
        or re.search(
            r'''(?i)\bSpawn-Service\s+(["'])bobclaw-core\1''', executable
        )
        is not None
        or (
            "Register-Wrapper 'BobClaw-Core'" in executable
            and "task-core.ps1" in executable
        )
        or "Start-ScheduledTask -TaskName 'BobClaw-Core'" in executable
        or "scripts\\win\\start-local.ps1" in executable
    )


def _powershell_launches_core(path: Path) -> bool:
    return _powershell_text_launches_core(path.read_text(encoding="utf-8"))


def _makefile_core_launch_targets(path: Path) -> set[str]:
    launch_targets: set[str] = set()
    current_target = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and ":" in line:
            current_target = line.split(":", 1)[0].strip()
            continue
        if not line.startswith("\t"):
            continue
        recipe = line.strip()
        if (
            current_target
            and "bobclaw-core" in recipe
            and re.search(r"\b(?:python|python3)\s+start\.py\b", recipe)
        ):
            launch_targets.add(current_target)
    return launch_targets


def _compose_launches_core(path: Path) -> bool:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services = document.get("services", {})
    if not isinstance(services, dict):
        return False
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        build = service.get("build", "")
        build_context = build.get("context", "") if isinstance(build, dict) else build
        identity_text = " ".join(
            (
                str(service_name),
                str(service.get("image", "")),
                str(build_context),
            )
        ).lower()
        command = service.get("command", "")
        entrypoint = service.get("entrypoint", "")
        command_text = " ".join(command) if isinstance(command, list) else str(command)
        entrypoint_text = (
            " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint)
        )
        if (
            "bobclaw-core" in identity_text
            or "start.py" in f"{entrypoint_text} {command_text}"
        ):
            return True
    return False


def _memory_capable_core_launchers() -> set[str]:
    """Inventory every repository entry path that can launch memory-enabled core."""
    found: set[str] = set()
    launcher_paths = [*REPO_ROOT.glob("*.ps1"), *SCRIPTS_WIN.glob("*.ps1")]
    for path in launcher_paths:
        if _powershell_launches_core(path):
            found.add(path.relative_to(REPO_ROOT).as_posix())

    makefile = REPO_ROOT / "Makefile"
    if _makefile_core_launch_targets(makefile):
        found.add("Makefile")

    compose = REPO_ROOT / "docker-compose.yml"
    if _compose_launches_core(compose):
        found.add("docker-compose.yml")
    return found


def test_memory_capable_core_launcher_inventory_is_complete():
    """New core launch paths must be classified by the bootstrap invariant."""
    assert _memory_capable_core_launchers() == LAUNCHER_INVENTORY


def test_powershell_tripwire_detects_executed_launcher_evasions():
    """Lock the auditor's three executed spellings into the inventory tripwire."""
    for script in (
        "python start.py",
        "&  .\\start.py",
        'Spawn-Service "bobclaw-core" "Core" "." "start.py"',
    ):
        assert _powershell_text_launches_core(script), script


def test_compose_is_parsed_and_does_not_launch_core():
    """Compose supplies dependencies only; host launchers own the core process."""
    assert _compose_launches_core(REPO_ROOT / "docker-compose.yml") is False


def test_makefile_start_target_is_inventory_covered():
    assert "start" in _makefile_core_launch_targets(REPO_ROOT / "Makefile")


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
