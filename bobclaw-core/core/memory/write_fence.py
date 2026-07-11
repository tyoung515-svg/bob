"""Single-writer fencing for one Qdrant collection family.

The fence holds one OS-level lock for the lifetime of a BoB memory collection family. Its identity
is the canonical Qdrant endpoint plus collection prefix, so a dimension migration remains under the
same lock. Family membership is deliberately strict: only ``<prefix>_<positive ASCII decimal>``
collections are writable. The provider imports that exact predicate for delete/scroll selection, so
provider selection is always a subset of fence authorization.

The fence is same-machine only. Cross-machine or containerized writers that reach the same endpoint
remain outside the OSS single-user deployment boundary; distributed exclusion requires a store-side
lease. Registry state is not a self-ACL for BoB's family: registration preserves collection uniqueness,
while construction fails closed if a non-BoB registry instance occupies any family collection. External
corpora retain their registry ACL path through ``lks_adapter``.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import ipaddress
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from core.ledger.federation import FederationRegistry
from core.memory.exceptions import ACLViolation
from core.memory.lks_adapter import InstanceACL
from core.memory.fingerprint import EmbedFingerprint, stamp_meta

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Write modes retained for federation-owned external corpus ACLs
# ---------------------------------------------------------------------------

_WRITE_MODES = frozenset({"rw", "w", "write", "read-write", "wo", "write-only"})


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class WriteFenceViolation(ACLViolation):
    """A write refused by the single-writer fence (an ACLViolation subclass)."""
    pass


# ---------------------------------------------------------------------------
# Federation ACL helper (external corpora remain on this explicit ACL path)
# ---------------------------------------------------------------------------

def enforce_write_acl(acl: InstanceACL, writer_id: str, *, context: str = "") -> None:
    """Raise unless an external instance ACL grants this writer write access."""
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


# ---------------------------------------------------------------------------
# Family identity and lock-directory helpers
# ---------------------------------------------------------------------------

_DEFAULT_QDRANT_PORT = 6333
_LOCK_DIR_ENV = "BOBCLAW_WRITE_FENCE_LOCK_DIR"
BOBCLAW_MEMORY_INSTANCE = "bobclaw-memory"
BOBCLAW_MEMORY_COLLECTION = "bobclaw__768"
BOBCLAW_OWNER = "bobclaw"
LKS_OWNER = "lks"


def is_collection_in_family(collection: str, prefix: str) -> bool:
    """Return whether *collection* is exactly one valid member of *prefix*'s family.

    The explicit ``[0-9]`` class is intentional: ``\\d`` would admit Unicode digits,
    and dimension zero is never an emitted Qdrant collection.
    """
    if not isinstance(collection, str) or not isinstance(prefix, str):
        return False
    return re.fullmatch(
        r"^" + re.escape(prefix) + r"_[1-9][0-9]*$", collection
    ) is not None


def _prefix_from_collection(collection: str) -> str:
    """Derive a family prefix from a valid collection for legacy constructor callers."""
    if not isinstance(collection, str) or not collection.strip():
        raise WriteFenceViolation(str(collection), "collection must be a non-empty string")
    match = re.fullmatch(r"(.+)_[1-9][0-9]*", collection.strip())
    if match is None:
        raise WriteFenceViolation(
            collection,
            "collection must end in a positive ASCII decimal dimension; pass collection_prefix explicitly",
        )
    return match.group(1)


def canonicalize_qdrant_url(qdrant_url: str) -> str:
    """Return the deterministic endpoint token used in the family lock identity.

    No DNS is consulted. ``localhost``, all IPv4 loopback aliases, and IPv6 loopback
    are folded to ``localhost``; non-loopback hosts retain their textual spelling.
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
    if host == "localhost":
        host = "localhost"
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and address.is_loopback:
            host = "localhost"
        elif ":" in host and not host.startswith("["):
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


def _is_programdata_lock_dir(lock_dir: Path) -> bool:
    """Return whether this is a ProgramData-backed machine-global lock directory."""
    if os.name != "nt":
        return False
    program_data = os.environ.get("ProgramData")
    if not program_data:
        return False
    try:
        lock_dir.resolve().relative_to(Path(program_data).resolve())
    except ValueError:
        return False
    return True


def _set_windows_lock_dir_acl(lock_dir: Path) -> None:
    """Grant a locale-independent explicit ACL to the new ProgramData lock directory."""
    command = [
        "icacls",
        str(lock_dir),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",       # LocalSystem
        "*S-1-5-32-544:(OI)(CI)F",   # BUILTIN\\Administrators
        "*S-1-5-32-545:(OI)(CI)M",   # BUILTIN\\Users: create/open/delete lock files
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise WriteFenceViolation(
            str(lock_dir),
            f"could not set explicit ProgramData write-lock ACL: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WriteFenceViolation(
            str(lock_dir),
            f"could not set explicit ProgramData write-lock ACL: {detail or result.returncode}",
        )


def _prepare_lock_dir(lock_dir: Path) -> Path:
    """Create and prove the lock directory is writable; never fall back elsewhere."""
    created = not lock_dir.exists()
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

    if created and _is_programdata_lock_dir(lock_dir):
        _set_windows_lock_dir_acl(lock_dir)

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


def _assert_lock_file_openable(lock_path: Path) -> None:
    """Surface ACL denial before filelock can compress it into a timeout."""
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
            raise PermissionError(exc.errno, exc.strerror, str(lock_path)) from exc
        raise
    else:
        os.close(descriptor)


def _is_permission_failure(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM)


# ---------------------------------------------------------------------------
# Registry family ownership validation
# ---------------------------------------------------------------------------

def assert_registry_family_available(
    registry: FederationRegistry, collection_prefix: str
) -> None:
    """Refuse external family ownership or misuse of BoB's reserved name."""
    try:
        records = registry.list()
    except Exception as exc:
        raise WriteFenceViolation(
            collection_prefix,
            f"could not inspect federation registry for family collisions: {exc}",
        ) from exc

    for record in records:
        name = record.get("name")
        collection = record.get("collection")
        in_family = is_collection_in_family(collection, collection_prefix)
        if name == BOBCLAW_MEMORY_INSTANCE:
            if not in_family:
                raise WriteFenceViolation(
                    collection_prefix,
                    f"reserved registry name {BOBCLAW_MEMORY_INSTANCE!r} owns "
                    f"out-of-family collection {collection!r}",
                )
        elif in_family:
            raise WriteFenceViolation(
                collection_prefix,
                f"registry collision: non-BoB instance {name!r} owns "
                f"family collection {collection!r}",
            )


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------

class WriteFence:
    """Hold one OS-enforced lock for a canonical Qdrant endpoint/collection family."""

    def __init__(
        self,
        registry: FederationRegistry,
        *,
        qdrant_url: str,
        collection: str | None = None,
        collection_prefix: str | None = None,
        owner: str = BOBCLAW_OWNER,
        lock_dir: str | Path | None = None,
    ) -> None:
        """Acquire the family lock or degrade read-only with an honest reason taxonomy.

        ``collection`` remains accepted for legacy callers but must identify a valid family
        member. New callers pass ``collection_prefix`` explicitly. A live lock holder is normal
        ``contention``; an ACL denial is ``permission``. Both remain fenced and refuse writes.
        """
        if collection_prefix is None:
            if collection is None:
                raise WriteFenceViolation(
                    "collection_prefix", "collection_prefix or a valid collection is required"
                )
            collection_prefix = _prefix_from_collection(collection)
        if not isinstance(collection_prefix, str) or not collection_prefix.strip():
            raise WriteFenceViolation(
                str(collection_prefix), "collection_prefix must be a non-empty string"
            )
        prefix = collection_prefix.strip()
        if collection is not None and not is_collection_in_family(collection.strip(), prefix):
            raise WriteFenceViolation(
                collection,
                f"collection is outside the protected family for prefix {prefix!r}",
            )

        self._registry = registry
        self._owner = owner
        self._collection_prefix = prefix
        try:
            canonical_url = canonicalize_qdrant_url(qdrant_url)
        except ValueError as exc:
            raise WriteFenceViolation(str(qdrant_url), str(exc)) from exc
        self._resource_identity = f"{canonical_url}|{self._collection_prefix}"
        self._assert_no_foreign_family_collision()
        self._lock_dir = _prepare_lock_dir(_resolve_lock_dir(lock_dir))
        digest = hashlib.sha256(self._resource_identity.encode("utf-8")).hexdigest()
        self._lock_path = self._lock_dir / digest
        self._lock = FileLock(self._lock_path, timeout=0)
        self._degraded = False
        self._degraded_reason = ""
        self._degraded_detail = ""
        try:
            _assert_lock_file_openable(self._lock_path)
            self._lock.acquire()
        except Timeout:
            self._set_degraded(
                "contention",
                "another same-machine writer holds the exclusive write lock",
            )
        except OSError as exc:
            if _is_permission_failure(exc):
                self._set_degraded(
                    "permission",
                    f"permission denied opening or locking {self._lock_path!s}: {exc}",
                )
            else:
                raise WriteFenceViolation(
                    self._resource_identity,
                    f"exclusive write lock unavailable at {self._lock_path!s}: {exc}",
                ) from exc
        except Exception as exc:
            raise WriteFenceViolation(
                self._resource_identity,
                f"exclusive write lock unavailable at {self._lock_path!s}: {exc}",
            ) from exc

    def _assert_no_foreign_family_collision(self) -> None:
        """Refuse a family whose registry namespace is not exclusively BoB's."""
        assert_registry_family_available(self._registry, self._collection_prefix)

    def _set_degraded(self, reason: str, detail: str) -> None:
        self._degraded = True
        self._degraded_reason = reason
        self._degraded_detail = detail
        log.warning(
            "Write fence degraded to read-only for resource %s: %s (%s); writes are refused",
            self._resource_identity,
            reason,
            detail,
        )

    @property
    def resource_identity(self) -> str:
        return self._resource_identity

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def collection_prefix(self) -> str:
        return self._collection_prefix

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    @property
    def degraded_detail(self) -> str:
        return self._degraded_detail

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
        """Release the held family lock; safe to call more than once."""
        if self._lock.is_locked:
            self._lock.release()

    def __enter__(self) -> "WriteFence":
        self._assert_lock_held(self._collection_prefix)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def assert_writable(self, collection: str) -> None:
        """Raise unless family membership and the held family lock permit the write."""
        if not isinstance(collection, str) or not collection.strip():
            raise WriteFenceViolation(
                str(collection), "collection must be a non-empty string"
            )
        coll = collection.strip()
        if not is_collection_in_family(coll, self._collection_prefix):
            raise WriteFenceViolation(
                coll,
                f"collection is outside protected family {self._collection_prefix!r}",
            )
        self._assert_lock_held(coll)


# ---------------------------------------------------------------------------
# Registration helpers (preserve registry collection uniqueness; no BoB self-ACL)
# ---------------------------------------------------------------------------

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
    """Register BoB's current collection fingerprint and retain registry uniqueness protection."""
    del readers  # Kept in the public signature for older callers; BoB family writes no longer self-ACL.
    meta = stamp_meta({}, fingerprint)
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
    """Stamp a read-only ACL onto each named external instance, preserving its meta."""
    for name in names:
        record = registry.get(name)
        meta = copy.deepcopy(record.get("meta") or {})
        meta["acl"] = {
            "writer": writer,
            "readers": list(readers),
            "mode": mode,
        }
        registry.update(name, meta=meta)
