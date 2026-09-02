from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping
from collections.abc import Mapping as MappingABC

from core.memory._hashing import blake3_hex, canonical_json
from core.memory.extractor import _normalize


def fingerprint(
    item: object,
    fields: Iterable[str] | None = None,
    *,
    normalize: bool = True,
) -> str:
    """Stable content fingerprint of one watch item."""
    if fields is not None:
        fields = tuple(fields)
    if isinstance(item, MappingABC) and fields is not None:
        payload = {k: item.get(k) for k in fields}
    elif isinstance(item, MappingABC):
        payload = dict(item)  # type: ignore[arg-type]
    else:
        payload = item

    if normalize:
        if isinstance(payload, str):
            payload = _normalize(payload)
        elif isinstance(payload, MappingABC):
            payload = {
                k: _normalize(v) if isinstance(v, str) else v
                for k, v in payload.items()
            }

    return blake3_hex(canonical_json(payload))


@dataclass(frozen=True)
class MarkedItem:
    item: object
    fingerprint: str
    seen: bool


def fingerprints_of(
    items: Iterable[object],
    fields: Iterable[str] | None = None,
    *,
    normalize: bool = True,
) -> set[str]:
    """Set of fingerprints for a corpus."""
    if fields is not None:
        fields = tuple(fields)  # materialize once: safe to reuse across items
    return {fingerprint(item, fields=fields, normalize=normalize) for item in items}


def mark_seen(
    items: Iterable[object],
    seen: Iterable[str],
    fields: Iterable[str] | None = None,
    *,
    normalize: bool = True,
) -> list[MarkedItem]:
    """Mark items as seen based on a pre-existing set and intra-batch deduplication."""
    if fields is not None:
        fields = tuple(fields)  # materialize once: safe to reuse across items
    seen_set = set(seen)
    batch_seen: set[str] = set()
    result: list[MarkedItem] = []
    for item in items:
        fp = fingerprint(item, fields=fields, normalize=normalize)
        is_seen = fp in seen_set or fp in batch_seen
        if not is_seen:
            batch_seen.add(fp)
        result.append(MarkedItem(item=item, fingerprint=fp, seen=is_seen))
    return result


def new_items(marked: list[MarkedItem]) -> list[object]:
    """First-seen items only."""
    return [m.item for m in marked if not m.seen]
