"""Parity guard: every key documented in `.secrets/bobclaw.env.example` must be a
real environment variable that some shipped module actually reads.

This catches the stale-example failure mode (an example key that no longer maps to
any `os.getenv`, e.g. a renamed provider URL/model), which silently misleads users.

The reverse direction is intentionally NOT asserted: the example lists the keys a
user typically sets and omits deep tuning knobs (COUNCIL_*/RESEARCH_*/BUILD_* etc.)
that have sane in-code defaults.
"""
from __future__ import annotations

import re
from pathlib import Path

# tests/ -> bobclaw-core/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / ".secrets" / "bobclaw.env.example"

_GETENV = re.compile(r'os\.(?:getenv|environ\.get)\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
_ENV_LINE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=')
# docker-compose env interpolation: ${VAR} / ${VAR:-default}
_COMPOSE_VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)')


def _example_keys() -> set[str]:
    keys: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _ENV_LINE.match(s)
        if m:
            keys.add(m.group(1))
    return keys


def _code_env_keys() -> set[str]:
    keys: set[str] = set()
    for py in _REPO_ROOT.rglob("*.py"):
        parts = set(py.parts)
        if ".venv" in parts or "tests" in parts:
            continue
        keys |= set(_GETENV.findall(py.read_text(encoding="utf-8", errors="ignore")))
    # docker-compose consumes some env vars directly (e.g. POSTGRES_PASSWORD).
    compose = _REPO_ROOT / "docker-compose.yml"
    if compose.exists():
        keys |= set(_COMPOSE_VAR.findall(compose.read_text(encoding="utf-8")))
    return keys


def test_env_example_has_no_orphan_keys() -> None:
    example = _example_keys()
    code = _code_env_keys()
    orphans = sorted(example - code)
    assert not orphans, (
        "These keys are in .secrets/bobclaw.env.example but read by no shipped module "
        f"(stale example?): {orphans}"
    )


def test_env_example_documents_the_core_secrets() -> None:
    # The three secrets the setup flow must populate — regression guard so a future
    # regeneration can't silently drop them.
    example = _example_keys()
    for required in ("BOBCLAW_SECRET", "BOBCLAW_PASSWORD", "ANTHROPIC_API_KEY", "POSTGRES_URL"):
        assert required in example, f"{required} missing from .env.example"
