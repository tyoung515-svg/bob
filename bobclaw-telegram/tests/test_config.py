"""bobclaw-telegram — config parsing tests (fail-closed allowlist)."""
from __future__ import annotations

import pytest

from bobclaw_telegram.config import ConfigError, DEFAULT_GATEWAY, load_config

_ENV = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_ALLOWED_USERS": "111, 222"}


def test_valid_config_parses(tmp_path):
    cfg = load_config(environ=_ENV, env_file=tmp_path / "missing.env")
    assert cfg.bot_token == "123:abc"
    assert cfg.allowed_users == frozenset({111, 222})
    assert cfg.gateway == DEFAULT_GATEWAY


def test_gateway_override(tmp_path):
    cfg = load_config(
        environ={**_ENV, "BOBCLAW_GATEWAY": "gw.example:9000"},
        env_file=tmp_path / "missing.env",
    )
    assert cfg.gateway == "gw.example:9000"


def test_missing_bot_token_fails(tmp_path):
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(
            environ={"TELEGRAM_ALLOWED_USERS": "111"},
            env_file=tmp_path / "missing.env",
        )


def test_missing_allowlist_fails_closed(tmp_path):
    with pytest.raises(ConfigError, match="TELEGRAM_ALLOWED_USERS"):
        load_config(environ={"TELEGRAM_BOT_TOKEN": "123:abc"}, env_file=tmp_path / "missing.env")


def test_empty_allowlist_fails_closed(tmp_path):
    for raw in ("", " , ,"):
        with pytest.raises(ConfigError, match="TELEGRAM_ALLOWED_USERS"):
            load_config(
                environ={"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_ALLOWED_USERS": raw},
                env_file=tmp_path / "missing.env",
            )


def test_non_numeric_allowlist_entry_fails_closed(tmp_path):
    # usernames / garbage are never accepted — the whole config is rejected
    for raw in ("@travis", "111,@travis", "abc", "111;222"):
        with pytest.raises(ConfigError, match="not a numeric user id"):
            load_config(
                environ={"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_ALLOWED_USERS": raw},
                env_file=tmp_path / "missing.env",
            )


def test_values_read_from_env_file(tmp_path):
    env_file = tmp_path / "bobclaw.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_ALLOWED_USERS=42\n"
        "BOBCLAW_GATEWAY=10.0.0.1:7836\n"
    )
    cfg = load_config(environ={}, env_file=env_file)
    assert cfg.bot_token == "123:abc"
    assert cfg.allowed_users == frozenset({42})
    assert cfg.gateway == "10.0.0.1:7836"


def test_env_file_crlf_tolerant(tmp_path):
    env_file = tmp_path / "bobclaw.env"
    env_file.write_bytes(
        b"TELEGRAM_BOT_TOKEN=123:abc\r\nTELEGRAM_ALLOWED_USERS=42\r\n"
    )
    cfg = load_config(environ={}, env_file=env_file)
    assert cfg.bot_token == "123:abc"
    assert cfg.allowed_users == frozenset({42})


def test_process_env_overrides_env_file(tmp_path):
    env_file = tmp_path / "bobclaw.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_ALLOWED_USERS=1\n")
    cfg = load_config(
        environ={"TELEGRAM_BOT_TOKEN": "env-token", "TELEGRAM_ALLOWED_USERS": "2"},
        env_file=env_file,
    )
    assert cfg.bot_token == "env-token"
    assert cfg.allowed_users == frozenset({2})
