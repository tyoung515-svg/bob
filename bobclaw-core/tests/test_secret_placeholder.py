"""Security guard: the placeholder VALUES shipped in .secrets/bobclaw.env.example
for gen_secrets-managed keys must be recognized by ``_envfile.is_placeholder``.

If a shipped placeholder isn't recognized, gen_secrets keeps it verbatim — and for
BOBCLAW_SECRET that means every default install signs JWTs / scope-vouch tokens
with a value printed in the public repo (forge-able auth). This is the regression
guard for that class of bug.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# tests/ -> bobclaw-core/ -> repo root -> scripts/_envfile.py
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / ".secrets" / "bobclaw.env.example"

_spec = importlib.util.spec_from_file_location(
    "_bob_envfile", _REPO_ROOT / "scripts" / "_envfile.py"
)
_envfile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_envfile)


def _example_values() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_bobclaw_secret_example_value_is_recognized_as_placeholder() -> None:
    assert _envfile.is_placeholder("change-me-run-gen_secrets")


def test_all_generated_secret_example_values_are_placeholders() -> None:
    values = _example_values()
    # Keys gen_secrets generates/manages — each must ship a value that gets regenerated.
    for key in ("BOBCLAW_SECRET", "TOTP_SECRET", "BOBCLAW_PASSWORD_HASH"):
        assert key in values, f"{key} missing from .env.example"
        assert _envfile.is_placeholder(values[key]), (
            f"{key}={values[key]!r} in .env.example is NOT a recognized placeholder "
            "-> gen_secrets would keep the public example value on install"
        )


def test_change_me_prefix_is_caught() -> None:
    # Future-proofing: any change-me-* example value is caught without editing the set.
    assert _envfile.is_placeholder("change-me-something-new")
    # A real generated secret must NOT read as a placeholder.
    assert not _envfile.is_placeholder("k3yZ8Qw2r-real-looking-secret-value")
