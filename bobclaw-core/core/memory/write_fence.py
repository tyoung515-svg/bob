"""MS2-C4 — single-writer write fence (OD#2/A) + BoB-memory registration helpers.

Additive, self-contained module for bobclaw-core (Python 3.13). Implements a fail-closed write fence that
permits a write ONLY to a collection whose federation-registry ACL declares the fence owner as the single
writer with a write-capable mode (single-writer-per-collection, DECISIONS-MS2 OD#2 = A). The fence *only
reads* from the registry — it never mutates a store or a client; an unregistered / un-owned / un-ACL'd /
garbled-ACL / non-write-mode target is REFUSED, not allowed. Closes the ``bobclaw__768``-in-LKS two-writer
footgun: BoB writes ONLY its own agent-memory collection; corpus collections (owned by the LKS rebuild) are
read-only to BoB (C3). Registration helpers stamp the C2 embed fingerprint + acl meta, then delegate to the
existing ``FederationRegistry.register``/``update`` (federation.py is NOT edited). Consumed by the
``QdrantRetrievalProvider`` optional ``write_fence`` seam (C4) and the C5/C6 cut-over.
"""
from __future__ import annotations

import copy
from typing import Iterable, Sequence

from core.ledger.federation import FederationRegistry, FederationError
from core.memory.exceptions import ACLViolation
from core.memory.lks_adapter import InstanceACL, read_instance_acl
from core.memory.fingerprint import EmbedFingerprint, stamp_meta


# ---------------------------------------------------------------------------
# Write modes that allow writes (fail-closed: unknown/read-only modes are denied)
# ---------------------------------------------------------------------------

_WRITE_MODES = frozenset({"rw", "w", "write", "read-write", "wo", "write-only"})


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class WriteFenceViolation(ACLViolation):
    """A write refused by the single-writer fence (an ACLViolation subclass — fail-closed)."""
    pass


# ---------------------------------------------------------------------------
# Enforcement helper
# ---------------------------------------------------------------------------

def enforce_write_acl(acl: InstanceACL, writer_id: str, *, context: str = "") -> None:
    """Raise WriteFenceViolation unless acl.mode permits writes AND acl.writer == writer_id."""
    # Normalize whitespace/case on BOTH sides of every comparison so trivial registry whitespace
    # (e.g. " rw" / " bobclaw ") can never spuriously DENY a legitimate owned write (mirrors the
    # reader/mode normalization C3's read_instance_acl already applies). A non-str/None writer fails closed.
    mode = acl.mode.strip().lower() if isinstance(acl.mode, str) else acl.mode
    if mode not in _WRITE_MODES:
        raise WriteFenceViolation(
            context or acl.writer or "instance",
            f"mode {acl.mode!r} does not permit writes",
        )
    if not isinstance(writer_id, str) or not writer_id.strip():
        raise WriteFenceViolation(
            context or "instance",
            "writer_id must be a non-empty string",
        )
    acl_writer = acl.writer.strip() if isinstance(acl.writer, str) else acl.writer
    if acl_writer != writer_id.strip():
        raise WriteFenceViolation(
            context or "instance",
            f"writer {acl.writer!r} != owner {writer_id!r}: single-writer cross-write refused",
        )
    # allow


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------

class WriteFence:
    """Refuse any write to a collection BoB does not own (single-writer-per-collection, OD#2/A)."""

    def __init__(self, registry: FederationRegistry, *, owner: str = "bobclaw") -> None:
        """Bind the federation registry (read-only) and the single-writer owner identity."""
        self._registry = registry
        self._owner = owner

    def assert_writable(self, collection: str) -> None:
        """Raise (fail-closed) unless `collection` resolves to an instance owned (writer) by self._owner."""
        # 1. collection must be a non-empty string
        if not isinstance(collection, str) or not collection.strip():
            raise WriteFenceViolation(
                str(collection),
                "collection must be a non-empty string",
            )
        coll = collection.strip()

        # 2. Registry reverse lookup — an unknown/unregistered collection is always refused
        try:
            record = self._registry.by_collection(coll)
        except FederationError as exc:
            raise WriteFenceViolation(
                coll,
                "collection not registered (single-writer-per-collection: "
                "an unowned/unknown collection is never writable)",
            ) from exc

        # 3. Read the ACL from meta. A present-but-garbled ACL is a refusal too — normalize the
        #    read_instance_acl ACLViolation to a WriteFenceViolation so the fence's refusal type is UNIFORM
        #    (a caller catching WriteFenceViolation catches garbled-ACL refusals too; since WriteFenceViolation
        #    IS an ACLViolation, base-class catchers keep working). Still strictly fail-closed.
        try:
            acl = read_instance_acl(record.get("meta"))
        except WriteFenceViolation:
            raise
        except ACLViolation as exc:
            raise WriteFenceViolation(coll, f"garbled acl: {exc.detail}") from exc
        except Exception as exc:
            # Defensive fail-closed: ANY other parse error (e.g. an unexpected non-dict meta type that
            # raises TypeError) is a REFUSAL, never an escape that would break the fail-closed invariant.
            raise WriteFenceViolation(
                coll, f"unparseable acl ({type(exc).__name__}): {exc}"
            ) from exc

        # 4. Refuse if no ACL is declared at all
        if acl is None:
            raise WriteFenceViolation(
                coll,
                "no acl declared for collection (fail-closed: refusing a write to an un-ACL'd collection)",
            )

        # 5. Enforce the write ACL (single-writer, mode check)
        enforce_write_acl(acl, self._owner, context=coll)


# ---------------------------------------------------------------------------
# Registration helpers (the API path; reuse C2 stamp_meta + the existing registry CRUD)
# ---------------------------------------------------------------------------

BOBCLAW_MEMORY_INSTANCE = "bobclaw-memory"
BOBCLAW_MEMORY_COLLECTION = "bobclaw__768"
BOBCLAW_OWNER = "bobclaw"
LKS_OWNER = "lks"


def register_bobclaw_memory(
    registry: FederationRegistry,
    fingerprint: EmbedFingerprint,
    *,
    collection: str = BOBCLAW_MEMORY_COLLECTION,
    readers: Sequence[str] = ("bobclaw",),
    repo: str = ".",
    ledger_dir: str = "ledger",
    overwrite: bool = False,
) -> dict:
    """Register the bobclaw-memory federation instance (writer=bobclaw, mode=rw) with a C2 embed fingerprint."""
    acl = {
        "writer": BOBCLAW_OWNER,
        "readers": list(readers),
        "mode": "rw",
    }
    meta = stamp_meta({"acl": acl}, fingerprint)
    return registry.register(
        BOBCLAW_MEMORY_INSTANCE,
        repo,
        collection=collection,
        dim=fingerprint.dim,
        ledger_dir=ledger_dir,
        meta=meta,
        overwrite=overwrite,
    )


def backfill_corpus_acl(
    registry: FederationRegistry,
    names: Iterable[str],
    *,
    writer: str = LKS_OWNER,
    readers: Sequence[str] = ("bobclaw", "lks"),
    mode: str = "ro",
) -> None:
    """Stamp a read-only ACL onto each named instance, PRESERVING its existing meta (note/embed)."""
    for name in names:
        record = registry.get(name)          # raises FederationError if unknown — let it propagate
        meta = copy.deepcopy(record.get("meta") or {})
        meta["acl"] = {
            "writer": writer,
            "readers": list(readers),
            "mode": mode,
        }
        registry.update(name, meta=meta)     # merges; preserves repo/collection/dim/ledger_dir
