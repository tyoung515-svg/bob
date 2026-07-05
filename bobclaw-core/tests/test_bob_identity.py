"""Spawn-identity card — every face is told it's running inside BoB (name / role / backend).

Default OFF ⇒ bob_identity_message returns None ⇒ byte-identical (no message added). ON ⇒ a
front-most system card naming the face + backend, so a face never claims it can't see its
deployment. Network-free.
"""
from __future__ import annotations

import core.config as cfg
from core.nodes.execute import BOB_IDENTITY_CARD, bob_identity_message


def test_off_by_default_returns_none(monkeypatch):
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_ENABLED", False)
    assert bob_identity_message({"face_id": "assistant", "backend": "local"}) is None


def test_on_injects_a_system_card_with_face_and_backend(monkeypatch):
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_ENABLED", True)
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_TEXT", "")
    msg = bob_identity_message({"face_id": "assistant", "backend": "deepseek_v4_flash"})
    assert msg is not None
    assert msg["role"] == "system"
    assert "BoB" in msg["content"]
    assert "deepseek_v4_flash" in msg["content"]          # the resolved backend is named
    assert "General Assistant" in msg["content"]          # the face's display name (registry)


def test_unknown_face_degrades_to_face_id(monkeypatch):
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_ENABLED", True)
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_TEXT", "")
    msg = bob_identity_message({"face_id": "no-such-face-xyz", "backend": "local"})
    assert msg is not None and "no-such-face-xyz" in msg["content"]  # never raises


def test_custom_template_override(monkeypatch):
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_ENABLED", True)
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_TEXT", "I am {face_name} on {backend}.")
    msg = bob_identity_message({"face_id": "assistant", "backend": "local"})
    assert msg["content"] == "I am General Assistant on local."


def test_bad_custom_template_falls_back_not_crash(monkeypatch):
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_ENABLED", True)
    # an unknown placeholder must not crash the turn — it falls back to the default card
    monkeypatch.setattr(cfg.config, "BOB_IDENTITY_TEXT", "broken {not_a_field}")
    msg = bob_identity_message({"face_id": "assistant", "backend": "local"})
    assert msg is not None and "BoB" in msg["content"]


def test_default_card_has_the_expected_placeholders():
    # guards drift: the shipped card must reference the three fields execute_node fills
    for field in ("{face_name}", "{role_clause}", "{backend}"):
        assert field in BOB_IDENTITY_CARD
