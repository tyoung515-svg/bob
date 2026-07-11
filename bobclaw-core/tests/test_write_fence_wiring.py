from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_WIN = REPO_ROOT / "scripts" / "win"


def test_every_memory_enabled_launcher_arms_the_write_fence():
    memory_assignment = re.compile(
        r"(?m)^\s*\$env:MEMORY_ENABLED\s*=\s*['\"]true['\"]"
    )
    fence_assignment = re.compile(
        r"(?m)^\s*\$env:MEMORY_WRITE_FENCE_ENABLED\s*=\s*['\"]true['\"]"
    )
    checked = []
    for path in sorted(SCRIPTS_WIN.glob("*.ps1")):
        text = path.read_text(encoding="utf-8")
        if memory_assignment.search(text):
            checked.append(path.name)
            assert fence_assignment.search(text), (
                f"{path.name} enables memory without enabling the write fence"
            )
    assert set(checked) == {"start-core.ps1", "task-core.ps1"}


def test_start_local_keeps_memory_and_write_fence_unforced():
    text = (SCRIPTS_WIN / "start-local.ps1").read_text(encoding="utf-8")
    assert not re.search(
        r"(?m)^\s*\$env:MEMORY_ENABLED\s*=", text
    )
    assert not re.search(
        r"(?m)^\s*\$env:MEMORY_WRITE_FENCE_ENABLED\s*=", text
    )


def test_requirements_lock_contains_installed_filelock_pin():
    lock = (REPO_ROOT / "bobclaw-core" / "requirements.lock").read_text(
        encoding="utf-8"
    )
    assert re.search(r"(?m)^filelock==3\.29\.5$", lock)
