"""bobclaw-telegram — auth gate decision tests (numeric ids only, never usernames)."""
from __future__ import annotations

from bobclaw_telegram.config import NOT_AUTHORIZED_REPLY, Config


def _cfg() -> Config:
    return Config(bot_token="t", allowed_users=frozenset({111, 222}))


def test_allowlisted_id_allowed():
    assert _cfg().is_allowed(111)
    assert _cfg().is_allowed(222)


def test_unknown_id_refused():
    assert not _cfg().is_allowed(333)


def test_none_refused():
    assert not _cfg().is_allowed(None)


def test_string_lookalike_refused():
    # the gate never coerces: a string "111" (or a username) is not the id 111
    assert not _cfg().is_allowed("111")
    assert not _cfg().is_allowed("@travis")
    assert not _cfg().is_allowed(True)  # bool is an int subclass — refuse it too


def test_refusal_reply_is_fixed():
    # fixed text, no per-user detail — changing it should be a deliberate act
    assert NOT_AUTHORIZED_REPLY == "not authorized"
