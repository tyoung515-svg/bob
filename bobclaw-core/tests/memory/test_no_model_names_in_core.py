from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent / "core" / "memory"

_FORBIDDEN: list[str] = [
    "granite",
    "gemma",
    "qwen3",
    "qwen2",
    "nomic",
    "bge-m3",
    "llama-",
    "claude-3",
    "claude-4",
    "gpt-4",
    "gpt-3.5",
]


def _iter_py_files() -> list[Path]:
    if not ROOT.is_dir():
        return []
    return list(ROOT.rglob("*.py"))


def test_no_model_names_in_core_code():
    """No model-name string appears in core/memory/ outside of slots.py and tests.

    The v4 invariant: model names live in config/memory_slots.toml, never in
    core code. The only exception is slots.py (the loader that reads the TOML).
    Lines starting with '# allowlisted-model-name:' are also exempt.
    """
    violations: list[tuple[Path, int, str]] = []
    for path in _iter_py_files():
        if path.name == "slots.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("# allowlisted-model-name:"):
                continue
            lower = line.lower()
            for token in _FORBIDDEN:
                if token in lower:
                    violations.append((path, lineno, line.strip()))
                    break
    if violations:
        msg_parts: list[str] = []
        for path, lineno, text in violations:
            msg_parts.append(f"  {path}:{lineno}: {text}")
        msg = "Model name strings found in core/memory/:\n" + "\n".join(msg_parts)
        msg += "\n\nModel names must live in config/memory_slots.toml, not in core code."
        pytest.fail(msg)
