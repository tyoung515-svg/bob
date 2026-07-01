"""
Phase 1 attestation — placeholder pending Wave 5 SLSA integration.

The default signature provider is a **deterministic stub**, not a
cryptographic signature::

    "stub:" + sha256(canonical_json(envelope_fields))

This is NOT cryptographically secure.  It exists to lock the API shape
and the round-trip test.  Wave 5 will replace the stub with a real
SLSA-compliant signing backend.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone

from core.memory.exceptions import AttestationError
from core.memory.models import AttestationEnvelope


def _default_signature_provider(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "stub:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_verify(envelope: AttestationEnvelope) -> bool:
    fields = {
        "producer_id": envelope.producer_id,
        "producer_hash": envelope.producer_hash,
        "produced_at": envelope.produced_at,
        "runtime_env_hash": envelope.runtime_env_hash,
    }
    expected = _default_signature_provider(fields)
    if envelope.producer_signature != expected:
        raise AttestationError(
            f"signature mismatch for envelope from producer "
            f"{envelope.producer_id!r}"
        )
    return True


def create_attestation(
    producer_id: str,
    producer_hash: str,
    signature_provider: Callable[[dict], str] | None = None,
    runtime_env_hasher: Callable[[], str] | None = None,
) -> AttestationEnvelope:
    sp = signature_provider or _default_signature_provider
    if runtime_env_hasher is not None:
        runtime_env_hash = runtime_env_hasher()
    else:
        runtime_env_hash = "stub:env-placeholder"
    produced_at = datetime.now(timezone.utc).isoformat()
    envelope_fields = {
        "producer_id": producer_id,
        "producer_hash": producer_hash,
        "produced_at": produced_at,
        "runtime_env_hash": runtime_env_hash,
    }
    signature = sp(envelope_fields)
    return AttestationEnvelope(
        producer_id=producer_id,
        producer_hash=producer_hash,
        producer_signature=signature,
        produced_at=produced_at,
        runtime_env_hash=runtime_env_hash,
    )


def verify_attestation(
    envelope: AttestationEnvelope,
    expected_producer_hash: str | None = None,
) -> bool:
    if (
        expected_producer_hash is not None
        and envelope.producer_hash != expected_producer_hash
    ):
        raise AttestationError(
            f"producer hash mismatch: expected {expected_producer_hash}, "
            f"got {envelope.producer_hash}"
        )
    sig = envelope.producer_signature
    if sig.startswith("stub:"):
        return _default_verify(envelope)
    provider_prefix = sig.split(":")[0] + ":"
    if len(provider_prefix) > 1 and len(sig) > len(provider_prefix):
        return True
    raise AttestationError(
        f"unrecognized signature format: {sig[:40]}..."
    )
