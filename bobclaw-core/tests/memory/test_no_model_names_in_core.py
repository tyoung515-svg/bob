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


CORE_DIR = Path(__file__).parent.parent.parent / "core"
STOCK_SLOTS = Path(__file__).parent.parent.parent / "config" / "memory_slots.toml"


def test_no_gemma_anywhere_in_core_or_stock_config():
    """gemma is retired from ALL of bob's pipelines (2026-07-20 directive,
    embedding-only posture). The hardwired local-routing preference and the
    stock slot defaults were purged 2026-07-24 after the name kept riding
    back in. It must not reappear in any core/ Python file (code, comments,
    or docstrings) or in config/memory_slots.toml.
    """
    targets = list(CORE_DIR.rglob("*.py")) if CORE_DIR.is_dir() else []
    if STOCK_SLOTS.is_file():
        targets.append(STOCK_SLOTS)
    violations: list[str] = []
    for path in targets:
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if "gemma" in line.lower():
                violations.append(f"  {path}:{lineno}: {line.strip()}")
    if violations:
        pytest.fail(
            "gemma reference(s) found — gemma is retired from all pipelines "
            "(embedding-only posture, 2026-07-20 directive):\n"
            + "\n".join(violations)
        )


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
