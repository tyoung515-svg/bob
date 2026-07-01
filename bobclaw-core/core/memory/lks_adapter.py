"""MS2-C3 — LKS retrieval adapter (read path through the federation resolver).

Additive, self-contained, READ-ONLY client. BoB reads a live LKS corpus collection
THROUGH the MS-1 federation resolver (``FederationRegistry.resolve``). Enforces the C2 embed
fingerprint fail-closed, a registry-declared read-only ACL, and consumes the C1 embedder for
text queries. Consumed by the research lane's LKS-first retriever (R1) and the C5 strangler
cut-over. See CONTRACTS-C3.md for the full contract.

READ-ONLY by construction: this class exposes no index/upsert/delete/write/create_collection
method and at runtime invokes only read-only client calls (``query_points`` / ``collection_exists``).
The Qdrant client is duck-typed and injected; ``qdrant_client`` is imported lazily inside the filter
helper, so the module imports with no qdrant installed.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from core.ledger.federation import FederationRegistry, ResolvedInstance, FederationError
from core.memory.fingerprint import (
    read_meta_fingerprint,
    assert_slot_matches_registry,
    FingerprintMissing,
    FingerprintMismatch,
    SENTINEL_POINT_ID,
    SENTINEL_MARKER_KEY,
)
from core.memory.models import Hit, SlotResolution
from core.memory.exceptions import ACLViolation

logger = logging.getLogger("bobclaw.memory.lks_adapter")


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class ReadAdapterError(RuntimeError):
    """Adapter misuse: no/both query args, k<=0, dim mismatch, search failure, unverifiable fingerprint."""
    pass


# ---------------------------------------------------------------------------
# Registry-declared read-only ACL (mirrors core/memory/acl.py shape)
# ---------------------------------------------------------------------------

_READ_MODES = frozenset({"ro", "rw", "r", "read", "read-only", "read-write"})


@dataclasses.dataclass(frozen=True)
class InstanceACL:
    """Read-only ACL for an instance, declared in the registry meta (writer, readers, mode)."""
    writer: Optional[str]
    readers: frozenset          # frozenset[str]; "*" means any reader
    mode: str                   # canonical lowercase


def read_instance_acl(meta: Optional[dict], *, key: str = "acl") -> Optional[InstanceACL]:
    """Extract InstanceACL from meta[key]; None if absent (legacy); ACLViolation if present-but-malformed."""
    if meta is None or key not in meta:
        return None
    block = meta[key]
    if not isinstance(block, dict):
        raise ACLViolation(key, "acl block must be a dict")
    # readers: a list/tuple of strings
    readers_raw = block.get("readers")
    if not isinstance(readers_raw, (list, tuple)):
        raise ACLViolation(key, "readers must be a list of strings")
    for r in readers_raw:
        if not isinstance(r, str) or not r.strip():
            # an empty/blank reader entry is malformed — it must never silently match a blank reader_id
            raise ACLViolation(key, "readers must be a list of non-empty strings")
    # mode: a non-empty string
    mode_raw = block.get("mode")
    if not isinstance(mode_raw, str) or not mode_raw:
        raise ACLViolation(key, "mode must be a non-empty string")
    # writer: None or a string
    writer = block.get("writer")
    if writer is not None and not isinstance(writer, str):
        raise ACLViolation(key, "writer must be None or a string")
    return InstanceACL(
        writer=writer,
        # normalize reader entries so trivial registry whitespace ("bobclaw ", "* ") cannot silently
        # break a wildcard or spuriously deny a legitimate reader.
        readers=frozenset(r.strip() for r in readers_raw),
        mode=mode_raw.strip().lower(),
    )


def enforce_read_acl(acl: InstanceACL, reader_id: str, *, context: str = "") -> None:
    """Raise ACLViolation unless acl.mode permits reads AND reader_id is allowed (fail-closed)."""
    if acl.mode not in _READ_MODES:
        raise ACLViolation(
            context or acl.writer or "instance",
            f"mode {acl.mode!r} does not permit reads",
        )
    if not isinstance(reader_id, str) or not reader_id.strip():
        # a blank/unidentified reader is never granted access, even against a "*" allowlist
        raise ACLViolation(context or "instance", "reader_id must be a non-empty string")
    rid = reader_id.strip()  # normalize symmetrically with the (stripped) reader entries
    if "*" in acl.readers:
        return
    if rid in acl.readers:
        return
    raise ACLViolation(
        context or "instance",
        f"reader {reader_id!r} not in readers allowlist",
    )


# ---------------------------------------------------------------------------
# The adapter (READ-ONLY LKS retrieval client)
# ---------------------------------------------------------------------------

class LKSReadAdapter:
    """Read-only client for a live LKS corpus collection through the federation resolver."""

    def __init__(
        self,
        registry: FederationRegistry,
        *,
        client,                                  # duck-typed sync Qdrant client (query_points / collection_exists)
        embedder=None,                           # async .embed(list[str]) -> list[list[float]] (C1 SlotResolvedEmbedder)
        live_slot: Optional[SlotResolution] = None,
        reader_id: str = "bobclaw",
        require_stamp: bool = True,
        # require_acl defaults False (NOT a fingerprint-style oversight): the adapter is STRUCTURALLY
        # read-only (no write path exists), so a missing/undeclared ACL can never escalate to a write;
        # an absent ACL on a structurally-read-only client returns CORRECT data to an unrestricted reader
        # (a least-privilege choice), unlike a same-dim embed swap which silently returns CORRUPT data
        # (a correctness catastrophe — hence require_stamp=True). False keeps the currently un-ACL'd
        # legacy instances readable for the C5 strangler cut-over; flip to True once C4 backfills meta.acl.
        # A DECLARED-but-garbled ACL still fails closed (read_instance_acl raises).
        require_acl: bool = False,
        normalize: bool = True,
        distance: str = "cosine",
    ) -> None:
        """Bind the registry, a duck-typed read-only client, optional embedder + live embedder slot, and policy."""
        self._registry = registry
        self._client = client
        self._embedder = embedder
        self._live_slot = live_slot
        self._reader_id = reader_id
        self._require_stamp = require_stamp
        self._require_acl = require_acl
        self._normalize = normalize
        self._distance = distance

    async def search(
        self,
        instance_name: str,
        *,
        query: Optional[str] = None,
        query_vector: Optional[list[float]] = None,
        k: int = 10,
        filters: Optional[dict] = None,
    ) -> list[Hit]:
        """Resolve, ACL-gate, fingerprint-gate, embed/inject, search, sentinel-filter — in that load-bearing order."""
        # 1. Resolve via the MS-1 federation registry (FederationError if unknown — let it propagate).
        resolved = self._registry.resolve(instance_name)

        # 2. ACL gate (read-only, registry-declared).
        acl = read_instance_acl(resolved.meta)
        if acl is None:
            if self._require_acl:
                raise ACLViolation(instance_name, "no acl declared and require_acl=True")
        else:
            enforce_read_acl(acl, self._reader_id, context=instance_name)

        # 3. Fingerprint gate (C2 fail-closed).
        meta_fp = read_meta_fingerprint(resolved.meta)
        if meta_fp is None:
            if self._require_stamp:
                raise FingerprintMissing(
                    f"no embed fingerprint stamp for instance {instance_name!r}"
                )
            # else: legacy soft path — proceed without verification.
        else:
            if self._live_slot is None:
                raise ReadAdapterError(
                    f"instance {instance_name!r} is fingerprint-stamped but no "
                    f"live_slot was provided to verify it (refusing to read unverified)"
                )
            assert_slot_matches_registry(
                resolved.meta,
                self._live_slot,
                normalize=self._normalize,
                distance=self._distance,
                require_stamp=self._require_stamp,
                context=instance_name,
            )

        # 4. Resolve the query vector (exactly one of query | query_vector).
        if (query is None) == (query_vector is None):
            raise ReadAdapterError("provide exactly one of query or query_vector")
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ReadAdapterError("k must be a positive int")
        if filters is not None and not isinstance(filters, dict):
            raise ReadAdapterError(f"filters must be a dict or None, got {type(filters).__name__}")

        if query_vector is not None:
            vec = list(query_vector)
        else:
            if self._embedder is None:
                raise ReadAdapterError("a text query requires an embedder")
            embedded = await self._embedder.embed([query])
            # A raised EmbedderUnavailable (embedder down) is meaningful + fail-closed and propagates
            # as-is; a malformed/empty RETURN must not crash opaquely (IndexError/TypeError) — fail closed.
            if not isinstance(embedded, (list, tuple)) or not embedded:
                raise ReadAdapterError(
                    f"embedder returned no vector for the text query (instance {instance_name!r})"
                )
            first = embedded[0]
            if not isinstance(first, (list, tuple)):
                raise ReadAdapterError(
                    f"embedder returned a malformed vector for the text query (instance {instance_name!r})"
                )
            vec = list(first)

        # Defense-in-depth: the registry validates dim to a positive int, but a garbled ResolvedInstance
        # must fail CLOSED (a clear ReadAdapterError) rather than crash the comparison with a TypeError.
        if not isinstance(resolved.dim, int) or isinstance(resolved.dim, bool) or resolved.dim <= 0:
            raise ReadAdapterError(
                f"instance {instance_name!r} has an invalid dim {resolved.dim!r} (cannot validate query vector)"
            )
        if len(vec) != resolved.dim:
            raise ReadAdapterError(
                f"query vector dim {len(vec)} != instance dim {resolved.dim} "
                f"for {instance_name!r}"
            )

        # Defense-in-depth: a garbled ResolvedInstance.collection must fail CLOSED with a clear error,
        # not surface as an opaque exception inside the Qdrant call.
        if not isinstance(resolved.collection, str) or not resolved.collection.strip():
            raise ReadAdapterError(
                f"instance {instance_name!r} has an invalid collection {resolved.collection!r}"
            )

        # 5. Search the collection (sentinel filtered, sorted, truncated to k).
        return self._search_collection(resolved.collection, vec, k, filters)

    def _search_collection(
        self, collection: str, vector: list[float], k: int, filters: Optional[dict]
    ) -> list[Hit]:
        """Run the Qdrant query_points, map to Hit, drop the C2 sentinel, sort desc, truncate to k."""
        try:
            qfilter = _build_filter(filters) if filters else None
        except Exception as exc:
            # a malformed filter or a missing-qdrant lazy ImportError must fail closed, not escape raw.
            raise ReadAdapterError(f"failed to build query filter: {exc}") from exc

        # Mirror QdrantRetrievalProvider: a missing collection means "no matches", not an error.
        try:
            if not self._client.collection_exists(collection):
                return []
        except Exception:
            # collection_exists itself failing is a real connectivity issue; fall through so
            # query_points raises a descriptive ReadAdapterError below.
            pass

        try:
            resp = self._client.query_points(
                collection_name=collection,
                query=vector,
                limit=k + 1,  # over-fetch so the sentinel filter still leaves k real hits
                query_filter=qfilter,
            )
            points = resp.points
        except Exception as exc:
            raise ReadAdapterError(
                f"search failed on collection {collection!r}: {exc}"
            ) from exc

        hits = [Hit(id=str(p.id), score=float(p.score), payload=(p.payload or {})) for p in points]

        # Drop the C2 sentinel point (by reserved id or marker) so it never pollutes retrieval.
        hits = [
            h for h in hits
            if str(h.id) != str(SENTINEL_POINT_ID) and not h.payload.get(SENTINEL_MARKER_KEY)
        ]

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


# ---------------------------------------------------------------------------
# Helper: build a Qdrant filter from a simple dict (lazy qdrant import)
# ---------------------------------------------------------------------------

def _build_filter(filters: dict) -> Any:
    """Build a Qdrant Filter from {key: value} / {key: list}; list -> MatchAny, scalar -> MatchValue."""
    from qdrant_client.http.models import (
        FieldCondition,
        Filter as QdrantFilter,
        MatchAny,
        MatchValue,
    )

    conditions = []
    for key, value in filters.items():
        if isinstance(value, (list, tuple)):
            conditions.append(FieldCondition(key=key, match=MatchAny(any=list(value))))
        else:
            conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
    if not conditions:
        return None
    return QdrantFilter(must=conditions)
