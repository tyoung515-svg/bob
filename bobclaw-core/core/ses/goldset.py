"""BoBClaw Core — SES: frozen claim goldset loader (V1 · Slice 0, F2).

Loads ``core/ses/data/claim_goldset_v1.json`` and VERIFIES its sha256 hash-pin: the digest
is computed over the canonical gold/extracted/distinct payload so no tuning run can silently
regenerate the ground truth (the red-team's F2 — measure the lift against a FROZEN set, never
one the new canonicalizer produced). Editing a claim changes the digest → load fails loud
until the pin is re-set deliberately.

This is the ONE ``core.ses`` module that does file I/O (a bounded read of a checked-in data
file via stdlib ``json``/``hashlib``/``pathlib`` — no model/network/clock/random). The
recall/precision/decompose buckets stay pure in-memory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.ses.types import SesError

_GOLDSET_PATH = Path(__file__).resolve().parent / "data" / "claim_goldset_v1.json"
_HASHED_KEYS = ("gold", "extracted", "distinct")


class GoldsetError(SesError):
    """The frozen goldset is missing, malformed, or its hash-pin does not verify."""


def goldset_digest(payload: dict) -> str:
    """The canonical sha256 over the ground-truth payload (only ``_HASHED_KEYS``, sorted).

    PURE + deterministic: editing a claim changes the digest (loud); editing ``_doc`` /
    ``schema_version`` does not. Same serialization the pin was minted with.
    """
    canon = {k: payload.get(k) for k in _HASHED_KEYS}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_goldset(*, verify: bool = True, path: Path = _GOLDSET_PATH) -> dict:
    """Load + hash-verify the frozen goldset; return the parsed dict.

    Raises :class:`GoldsetError` on a missing/malformed file or (when ``verify``) a hash-pin
    mismatch — the frozen ground truth must never drift silently.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldsetError(f"goldset not readable at {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoldsetError(f"goldset is not valid JSON: {exc}") from exc
    for k in _HASHED_KEYS:
        if not isinstance(data.get(k), list):
            raise GoldsetError(f"goldset missing/invalid {k!r} list")
    if verify:
        want, got = data.get("sha256"), goldset_digest(data)
        if want != got:
            raise GoldsetError(
                f"goldset hash-pin mismatch: stored {want!r} != computed {got!r} "
                "(the frozen ground truth was edited — re-pin deliberately, never silently)"
            )
    return data


__all__ = ["GoldsetError", "goldset_digest", "load_goldset", "_GOLDSET_PATH"]
