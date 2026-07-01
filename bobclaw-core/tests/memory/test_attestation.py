from __future__ import annotations

import json

import pytest

from core.memory.attestation import (
    _default_signature_provider,
    create_attestation,
    verify_attestation,
)
from core.memory.exceptions import AttestationError
from core.memory.models import AttestationEnvelope


def test_create_attestation_constructs_envelope():
    env = create_attestation(
        producer_id="test-producer",
        producer_hash="abc123",
    )
    assert isinstance(env, AttestationEnvelope)
    assert env.producer_id == "test-producer"
    assert env.producer_hash == "abc123"
    assert env.producer_signature.startswith("stub:")
    assert env.produced_at is not None
    assert env.runtime_env_hash == "stub:env-placeholder"


def test_verify_with_stub_provider_succeeds():
    env = create_attestation(
        producer_id="test-producer",
        producer_hash="abc123",
    )
    assert verify_attestation(env) is True


def test_verify_tampered_envelope_raises():
    env = create_attestation(
        producer_id="test-producer",
        producer_hash="abc123",
    )
    tampered = AttestationEnvelope(
        producer_id=env.producer_id,
        producer_hash="tampered",
        producer_signature=env.producer_signature,
        produced_at=env.produced_at,
        runtime_env_hash=env.runtime_env_hash,
    )
    with pytest.raises(AttestationError):
        verify_attestation(tampered, expected_producer_hash="original-hash")


def test_verify_producer_hash_mismatch_raises():
    env = create_attestation(
        producer_id="test-producer",
        producer_hash="abc123",
    )
    with pytest.raises(AttestationError):
        verify_attestation(env, expected_producer_hash="wrong-hash")


def test_envelope_is_frozen():
    env = create_attestation(
        producer_id="test-producer",
        producer_hash="abc123",
    )
    with pytest.raises(AttributeError):
        env.producer_id = "hacked"


def test_roundtrip_json():
    env = create_attestation(
        producer_id="test-producer",
        producer_hash="abc123",
    )
    data = json.dumps({
        "producer_id": env.producer_id,
        "producer_hash": env.producer_hash,
        "producer_signature": env.producer_signature,
        "produced_at": env.produced_at,
        "runtime_env_hash": env.runtime_env_hash,
    })
    restored = json.loads(data)
    restored_env = AttestationEnvelope(**restored)
    assert verify_attestation(restored_env) is True


def test_custom_signature_provider():
    def _fake_provider(fields: dict) -> str:
        return "fake:always-valid"

    env = create_attestation(
        producer_id="custom-producer",
        producer_hash="def456",
        signature_provider=_fake_provider,
    )
    assert env.producer_signature == "fake:always-valid"
    assert verify_attestation(env) is True
