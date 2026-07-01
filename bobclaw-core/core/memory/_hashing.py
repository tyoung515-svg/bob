from __future__ import annotations

import json
import unicodedata
from typing import Any

GENERATION_METHOD_ALLOWLISTS: dict[str, frozenset[str]] = {
    "extract_facts_from_event": frozenset({
        "event.body", "event.kind", "extractor.version", "prompt.version",
    }),
    "splice_section": frozenset({
        "facts[].id", "facts[].body_hash", "section_mapping.version",
    }),
    "render_wiki": frozenset({
        "section.id", "section.body_hash", "template.version",
    }),
    "crystallize_session": frozenset({
        "session.id", "session.event_ids", "crystallizer.version", "prompt.version",
    }),
    "rollup_mid": frozenset({
        "children[].id", "children[].body_hash", "rollup.spec.version",
    }),
    "audit_pass": frozenset({
        "audit.scope", "audit.targets[].id", "audit.targets[].body_hash", "auditor.version",
    }),
}


def _nfc_normalize(obj: Any) -> Any:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, dict):
        return {_nfc_normalize(k): _nfc_normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nfc_normalize(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> bytes:
    normalized = _nfc_normalize(obj)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def blake3_hex(data: bytes) -> str:
    import blake3
    return "blake3:" + blake3.blake3(data).hexdigest()


def compute_input_hash(generation_method: str, inputs: dict) -> str:
    from core.memory.exceptions import HashAllowlistMissing, HashingError
    allowlist = GENERATION_METHOD_ALLOWLISTS.get(generation_method)
    if allowlist is None:
        raise HashAllowlistMissing(generation_method)
    extra_keys = set(inputs.keys()) - allowlist
    if extra_keys:
        raise HashingError(
            f"extra keys not in allowlist for {generation_method!r}: {sorted(extra_keys)}"
        )
    return blake3_hex(canonical_json(inputs))


def _compute_event_hash(body: dict, prev_hash: str | None) -> str:
    canonical = canonical_json(body)
    prev_bytes = prev_hash.encode("utf-8") if prev_hash else b""
    return blake3_hex(canonical + prev_bytes)


def verify_event_hash(event, prev_hash: str | None) -> bool:
    return event.hash == _compute_event_hash(event.body, prev_hash)
