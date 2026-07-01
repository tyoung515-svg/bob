"""Shared env-file helper for BoBClaw setup scripts.

Reads / updates `.secrets/bobclaw.env` while preserving comments, blank lines,
and ordering. Only modifies the keys explicitly passed in.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def env_path() -> Path:
    return repo_root() / ".secrets" / "bobclaw.env"


def load(path: Path | None = None) -> dict[str, str]:
    p = path or env_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def update(updates: dict[str, str], path: Path | None = None) -> list[str]:
    """Apply key=value updates to the env file. Preserves comments / order.

    Returns a list of human-readable change descriptions.
    """
    p = path or env_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")

    lines = p.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    changes: list[str] = []

    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, _ = s.partition("=")
        k = k.strip()
        if k in updates:
            new_val = updates[k]
            lines[i] = f"{k}={new_val}"
            seen.add(k)
            changes.append(f"updated {k}")

    appended: list[str] = [k for k in updates if k not in seen]
    if appended:
        if lines and lines[-1].strip():
            lines.append("")
        for k in appended:
            lines.append(f"{k}={updates[k]}")
            changes.append(f"appended {k}")

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "",
    "change-me",
    "change-me-to-a-random-string",
    "change-me-run-gen_secrets",  # the value .secrets/bobclaw.env.example ships for BOBCLAW_SECRET
    "changeme",
    "secret",
    "password",
    "bobclaw",
    "None",
    "sk-ant-...",
    "AIza...",
})


def is_placeholder(value: str) -> bool:
    """True if ``value`` is an unset/example placeholder that must be regenerated.

    Exact-match against ``PLACEHOLDER_VALUES`` plus a ``change-me`` prefix guard so
    any future ``change-me-*`` example value is caught without editing this set.
    A placeholder that isn't recognized here is kept verbatim by gen_secrets —
    which, for BOBCLAW_SECRET, would ship a publicly-known JWT/scope-vouch key.
    """
    v = value.strip()
    return v in PLACEHOLDER_VALUES or v.startswith("change-me")


def keys_needing_value(env: dict[str, str], keys: Iterable[str]) -> list[str]:
    return [k for k in keys if k not in env or is_placeholder(env[k])]
