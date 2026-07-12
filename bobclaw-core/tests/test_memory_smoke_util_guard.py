"""Regression guards for the write-capable memory-smoke isolation helpers
(tests/_memory_smoke_util.py). These are the safety rails, so they get their own
unit tests — a bug here silently lets a live test hit the shared LKS Qdrant."""
from __future__ import annotations

import pytest

from tests import _memory_smoke_util as util


def test_default_url_is_bob_6353(monkeypatch):
    monkeypatch.delenv("MEMORY_QDRANT_URL", raising=False)
    assert util.resolve_bob_qdrant_url() == "http://localhost:6353"


def test_rejects_6333_by_default(monkeypatch):
    monkeypatch.setenv("MEMORY_QDRANT_URL", "http://localhost:6333")
    monkeypatch.delenv("MEMORY_TEST_ALLOW_6333", raising=False)
    with pytest.raises(RuntimeError):
        util.resolve_bob_qdrant_url()


@pytest.mark.parametrize("falsey", ["0", "false", "", "no", "off"])
def test_falsey_optin_does_not_permit_6333(monkeypatch, falsey):
    """The bug: a bare truthiness check let '0' through. A falsey opt-in must NOT
    permit the LKS Qdrant."""
    monkeypatch.setenv("MEMORY_QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("MEMORY_TEST_ALLOW_6333", falsey)
    with pytest.raises(Exception):
        util.resolve_bob_qdrant_url()


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE"])
def test_truthy_optin_permits_6333(monkeypatch, truthy):
    monkeypatch.setenv("MEMORY_QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("MEMORY_TEST_ALLOW_6333", truthy)
    assert util.resolve_bob_qdrant_url() == "http://localhost:6333"
