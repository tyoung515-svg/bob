"""bobclaw-telegram — env-driven configuration (fail-closed).

Values come from the process environment first, then ``.secrets/bobclaw.env``
at the repo root (the same shared env file the gateway uses; CRLF-tolerant
because it is shared with Windows tooling).

- ``TELEGRAM_BOT_TOKEN`` (required) — BotFather token.
- ``TELEGRAM_ALLOWED_USERS`` (required) — comma-separated *numeric* Telegram
  user ids. Fail-closed: empty, missing, or any non-numeric entry is a fatal
  startup error. Usernames are never accepted.
- ``BOBCLAW_GATEWAY`` (optional, default ``127.0.0.1:7836``).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = _REPO_ROOT / ".secrets" / "bobclaw.env"
DEFAULT_GATEWAY = "127.0.0.1:7836"

# Fixed refusal text for non-allowlisted users (no details leaked).
NOT_AUTHORIZED_REPLY = "not authorized"


class ConfigError(ValueError):
    """Missing/invalid configuration — startup must refuse to run."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_users: frozenset[int]
    gateway: str = field(default=DEFAULT_GATEWAY)

    def is_allowed(self, user_id: object) -> bool:
        """Auth gate: allowlisted numeric user ids only — never usernames.

        A string that merely *looks* numeric (or a username) is refused: the
        gate compares the integer id Telegram asserts, nothing else.
        """
        return isinstance(user_id, int) and not isinstance(user_id, bool) \
            and user_id in self.allowed_users


def _parse_allowed_users(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ConfigError(
                f"TELEGRAM_ALLOWED_USERS: {part!r} is not a numeric user id "
                "(usernames are never accepted)"
            )
        ids.add(int(part))
    if not ids:
        raise ConfigError(
            "TELEGRAM_ALLOWED_USERS is required and must list at least one "
            "numeric Telegram user id (fail-closed)"
        )
    return frozenset(ids)


def _read_env_file(env_file: Path | str) -> dict[str, str]:
    path = Path(env_file)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:
        raise ConfigError(f"cannot read env file {path}: {exc}")
    return {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}


def load_config(
    environ: dict[str, str] | None = None,
    env_file: Path | str = ENV_FILE,
) -> Config:
    """Build the config or raise ConfigError. Process env wins over the file."""
    file_vals = _read_env_file(env_file)
    env = os.environ if environ is None else environ

    def _get(key: str, default: str = "") -> str:
        return (env.get(key) or file_vals.get(key) or default).strip()

    bot_token = _get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is required (set it in the environment or "
            f"{ENV_FILE})"
        )
    allowed_users = _parse_allowed_users(_get("TELEGRAM_ALLOWED_USERS"))
    gateway = _get("BOBCLAW_GATEWAY", DEFAULT_GATEWAY) or DEFAULT_GATEWAY
    return Config(bot_token=bot_token, allowed_users=allowed_users, gateway=gateway)
