from __future__ import annotations

import unicodedata

import pytest

from core.memory._hashing import (
    GENERATION_METHOD_ALLOWLISTS,
    blake3_hex,
    canonical_json,
    compute_input_hash,
    verify_event_hash,
)
from core.memory.exceptions import HashAllowlistMissing, HashingError
from core.memory.models import Event


class TestCanonicalJson:
    def test_same_dict_different_order_gives_same_bytes(self):
        d1 = {"z": 1, "a": 2, "m": 3}
        d2 = {"a": 2, "m": 3, "z": 1}
        assert canonical_json(d1) == canonical_json(d2)

    def test_nfc_normalization_produces_same_bytes(self):
        nfd = unicodedata.normalize("NFD", "café")
        nfc = unicodedata.normalize("NFC", "café")
        assert nfd != nfc
        d1 = {"text": nfd}
        d2 = {"text": nfc}
        assert canonical_json(d1) == canonical_json(d2)


class TestBlake3Hex:
    def test_deterministic_across_runs(self):
        data = b"hello world"
        h1 = blake3_hex(data)
        h2 = blake3_hex(data)
        assert h1 == h2

    def test_format_blake3_prefix(self):
        h = blake3_hex(b"test")
        assert h.startswith("blake3:")
        assert len(h) == 7 + 64  # "blake3:" + 64 hex chars


class TestComputeInputHash:
    def test_unknown_generation_method_raises(self):
        with pytest.raises(HashAllowlistMissing):
            compute_input_hash("nonexistent_method", {})

    def test_extra_keys_raises(self):
        with pytest.raises(HashingError):
            compute_input_hash(
                "extract_facts_from_event",
                {"event.extraction_input": "x", "event.kind": "y", "unexpected_key": "z"},
            )

    def test_bit_identical_for_same_canonical_inputs(self):
        h1 = compute_input_hash(
            "extract_facts_from_event",
            {"event.extraction_input": {"text": "hi"}, "event.kind": "observation"},
        )
        h2 = compute_input_hash(
            "extract_facts_from_event",
            {"event.kind": "observation", "event.extraction_input": {"text": "hi"}},
        )
        assert h1 == h2


class TestVerifyEventHash:
    def test_returns_true_for_valid_triple(self):
        body = {"text": "test event"}
        event = Event(
            event_id="evt_001",
            kind="observation",
            body=body,
            ts="2026-05-12T00:00:00Z",
            hash="",
            prev_hash=None,
        )
        expected_hash = _compute_event_hash_for_test(body, None)
        event = event.__class__(
            event_id=event.event_id,
            kind=event.kind,
            body=event.body,
            ts=event.ts,
            hash=expected_hash,
            prev_hash=event.prev_hash,
        )
        assert verify_event_hash(event, None) is True

    def test_returns_false_when_body_mutated(self):
        body = {"text": "original"}
        event = Event(
            event_id="evt_002",
            kind="observation",
            body=body,
            ts="2026-05-12T00:00:00Z",
            hash="",
            prev_hash=None,
        )
        expected_hash = _compute_event_hash_for_test(body, None)
        mutated = Event(
            event_id=event.event_id,
            kind=event.kind,
            body={"text": "tampered"},
            ts=event.ts,
            hash=expected_hash,
            prev_hash=event.prev_hash,
        )
        assert verify_event_hash(mutated, None) is False


def _compute_event_hash_for_test(body: dict, prev_hash: str | None) -> str:
    from core.memory._hashing import canonical_json, blake3_hex
    canonical = canonical_json(body)
    prev_bytes = prev_hash.encode("utf-8") if prev_hash else b""
    return blake3_hex(canonical + prev_bytes)
