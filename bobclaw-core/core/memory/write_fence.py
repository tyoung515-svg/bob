"""Single-writer fencing for one Qdrant resource.

The fence authorizes BoB's registry ACL and holds one OS-level lock for the lifetime of the
fence. The lock is keyed by the protected resource, not by an install directory: its identity
is the canonical Qdrant endpoint plus collection name. A contending same-machine writer starts
as a read-only replica and receives ``WriteFenceViolation`` at the write call.

This is a same-machine writer fence only. Cross-machine or containerized writers that reach the
same endpoint are explicitly out of scope for the OSS single-user deployment; a store-side lease
would be required for distributed exclusion.

Registration helpers stamp the C2 embed fingerprint plus ACL metadata, then delegate to the
existing federation registry CRUD. The Qdrant provider uses this module as its optional write
seam; reads do not require the held write lock.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
from pathlib import Path
import tempfile
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from core.ledger.federation import FederationRegistry, FederationError
from core.memory.exceptions import ACLViolation
from core.memory.lks_adapter import InstanceACL, read_instance_acl
from core.memory.fingerprint import EmbedFingerprint, stamp_meta

log = logging.getLogger(__name__)

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

_DEFAULT_QDRANT_PORT = 6333
_LOCK_DIR_ENV = "BOBCLAW_WRITE_FENCE_LOCK_DIR"


def canonicalize_qdrant_url(qdrant_url: str) -> str:
    """Return the canonical endpoint form used in the lock identity.

    Canonicalization lowercases the scheme and host, makes Qdrant's default port 6333
    explicit, strips a trailing slash from the path, and preserves a non-empty path/query.
    For example, ``http://localhost:6333`` and ``http://LOCALHOST/`` both become
    ``http://localhost:6333``.
    """
    if not isinstance(qdrant_url, str) or not qdrant_url.strip():
        raise ValueError("qdrant_url must be a non-empty URL")

    parsed = urlsplit(qdrant_url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"qdrant_url must include a scheme and host: {qdrant_url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("qdrant_url must not include userinfo")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or _DEFAULT_QDRANT_PORT
    path = parsed.path.rstrip("/")
    suffix = path
    if parsed.query:
        suffix += f"?{parsed.query}"
    return f"{scheme}://{host}:{port}{suffix}"


def _resolve_lock_dir(lock_dir: str | Path | None) -> Path:
    """Resolve the machine-global lock directory, with a test-only env override."""
    if lock_dir is not None:
        return Path(lock_dir).expanduser().resolve()

    override = os.environ.get(_LOCK_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        program_data = os.environ.get("ProgramData")
        if not program_data:
            raise WriteFenceViolation(
                r"%ProgramData%\bobclaw\locks",
                "ProgramData is not set; cannot resolve the machine-global write-lock directory",
            )
        return (Path(program_data) / "bobclaw" / "locks").resolve()

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return (Path(runtime_dir) / "bobclaw").resolve()
    return Path("/var/lock/bobclaw").resolve()


def _prepare_lock_dir(lock_dir: Path) -> Path:
    """Create and prove the lock directory is writable; never fall back elsewhere."""
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise WriteFenceViolation(
            str(lock_dir),
            f"write-lock directory could not be created: {exc}",
        ) from exc

    if not lock_dir.is_dir():
        raise WriteFenceViolation(
            str(lock_dir),
            "write-lock path exists but is not a directory",
        )

    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=".bobclaw-write-probe-", dir=lock_dir, delete=False
        ) as probe:
            probe_path = Path(probe.name)
    except Exception as exc:
        raise WriteFenceViolation(
            str(lock_dir),
            f"write-lock directory is not writable: {exc}",
        ) from exc
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass
    return lock_dir


class WriteFence:
    """Hold an OS-enforced lock for one canonical Qdrant endpoint/collection."""

    def __init__(
        self,
        registry: FederationRegistry,
        *,
        qdrant_url: str,
        collection: str,
        owner: str = "bobclaw",
        lock_dir: str | Path | None = None,
    ) -> None:
        """Bind ACL policy and acquire one resource lock for this fence lifetime.

        A lock conflict is a normal multi-process deployment state: the losing process keeps
        its read path alive and becomes write-degraded. Any lock-directory or other I/O failure
        remains fail-closed and raises during construction.
        """
        if not isinstance(collection, str) or not collection.strip():
            raise WriteFenceViolation(
                str(collection),
                "collection must be a non-empty string",
            )

        self._registry = registry
        self._owner = owner
        self._collection = collection.strip()
        try:
            canonical_url = canonicalize_qdrant_url(qdrant_url)
        except ValueError as exc:
            raise WriteFenceViolation(str(qdrant_url), str(exc)) from exc
        self._resource_identity = f"{canonical_url}|{self._collection}"
        self._lock_dir = _prepare_lock_dir(_resolve_lock_dir(lock_dir))
        digest = hashlib.sha256(self._resource_identity.encode("utf-8")).hexdigest()
        self._lock_path = self._lock_dir / digest
        self._lock = FileLock(self._lock_path, timeout=0)
        self._degraded = False
        self._degraded_reason = ""
        try:
            self._lock.acquire()
        except Timeout as exc:
            self._degraded = True
            self._degraded_reason = (
                "another same-machine writer holds the exclusive write lock"
            )
            log.warning(
                "Write fence degraded to read-only for resource %s: %s; writes are refused",
                self._resource_identity,
                self._degraded_reason,
            )
        except Exception as exc:
            raise WriteFenceViolation(
                self._resource_identity,
                f"exclusive write lock unavailable at {self._lock_path!s}: {exc}",
            ) from exc

    @property
    def resource_identity(self) -> str:
        return self._resource_identity

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def collection(self) -> str:
        return self._collection

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    def _assert_lock_held(self, resource: str) -> None:
        if self._degraded:
            raise WriteFenceViolation(
                resource,
                f"writes refused: {self._degraded_reason} for resource {self._resource_identity}",
            )
        if not self._lock.is_locked:
            raise WriteFenceViolation(
                resource,
                f"exclusive write lock is not held for resource {self._resource_identity}",
            )

    def close(self) -> None:
        """Release the held resource lock; safe to call more than once."""
        if self._lock.is_locked:
            self._lock.release()

    def __enter__(self) -> "WriteFence":
        self._assert_lock_held(self._collection)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def assert_writable(self, collection: str) -> None:
        """Raise unless ACL authorization and this fence's held resource lock permit the write."""
        if not isinstance(collection, str) or not collection.strip():
            raise WriteFenceViolation(
                str(collection),
                "collection must be a non-empty string",
            )
        coll = collection.strip()
        if coll != self._collection:
            raise WriteFenceViolation(
                coll,
                f"resource mismatch: held lock covers {self._resource_identity}, not collection {coll!r}",
            )
        self._assert_lock_held(coll)

        try:
            record = self._registry.by_collection(coll)
        except FederationError as exc:
            raise WriteFenceViolation(
                coll,
                "collection not registered (single-writer-per-collection: "
                "an unowned/unknown collection is never writable)",
            ) from exc

        try:
            acl = read_instance_acl(record.get("meta"))
        except WriteFenceViolation:
            raise
        except ACLViolation as exc:
            raise WriteFenceViolation(coll, f"garbled acl: {exc.detail}") from exc
        except Exception as exc:
            raise WriteFenceViolation(
                coll, f"unparseable acl ({type(exc).__name__}): {exc}"
            ) from exc

        if acl is None:
            raise WriteFenceViolation(
                coll,
                "no acl declared for collection (fail-closed: refusing a write to an un-ACL'd collection)",
            )

        enforce_write_acl(acl, self._owner, context=coll)
        self._assert_lock_held(coll)

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
