"""
BoBClaw Claude Build Pipeline — Configuration
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env files — try shared secrets first, then local fallback
# ---------------------------------------------------------------------------

_SECRETS_PATH = Path(__file__).resolve().parents[1] / ".secrets" / "bobclaw.env"
_LOCAL_ENV_PATH = Path(__file__).resolve().parent / ".env"

for _env_file in (_SECRETS_PATH, _LOCAL_ENV_PATH):
    if _env_file.exists():
        load_dotenv(dotenv_path=_env_file, override=False)
        break

# ---------------------------------------------------------------------------
# Required
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

if not ANTHROPIC_API_KEY:
    import warnings
    warnings.warn(
        "ANTHROPIC_API_KEY is not set. Set it in the environment, "
        "../../.secrets/bobclaw.env, or a local .env file.",
        stacklevel=1,
    )

# JWT secret shared with gateway for validating access tokens
JWT_SECRET: str = os.environ.get("JWT_SECRET") or os.environ.get("BOBCLAW_SECRET", "")

if not JWT_SECRET:
    import warnings
    warnings.warn(
        "JWT_SECRET is not set. Pipeline endpoints will reject all requests.",
        stacklevel=1,
    )

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

PORT: int = int(os.environ.get("PORT", 7823))
# Loopback by default — this service holds an ANTHROPIC_API_KEY and must not be
# exposed on a routable interface. See SECURITY.md.
HOST: str = os.environ.get("HOST", "127.0.0.1")

# ---------------------------------------------------------------------------
# Build limits
# ---------------------------------------------------------------------------

MAX_CONCURRENT_BUILDS: int = int(os.environ.get("MAX_CONCURRENT_BUILDS", 3))
BUILD_TIMEOUT_SECONDS: int = int(os.environ.get("BUILD_TIMEOUT_SECONDS", 300))

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# Current-generation Claude model IDs (see https://platform.claude.com/docs/en/about-claude/models).
# Add or change these to whatever your Anthropic account has access to.
ALLOWED_MODELS: list[str] = [
    "claude-sonnet-5",
    "claude-opus-4-8",
]

DEFAULT_MODEL: str = os.environ.get("DEFAULT_MODEL", "claude-sonnet-5")

if DEFAULT_MODEL not in ALLOWED_MODELS:
    raise ValueError(
        f"DEFAULT_MODEL '{DEFAULT_MODEL}' is not in ALLOWED_MODELS: {ALLOWED_MODELS}"
    )

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Build workspace root (sandboxed)
# ---------------------------------------------------------------------------

BUILD_WORKSPACE_ROOT: Path = Path(os.environ.get("BUILD_WORKSPACE_ROOT", "/tmp/bobclaw-builds"))
