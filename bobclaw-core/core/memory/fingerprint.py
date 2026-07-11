"""Embed fingerprint (version-stamp collections) — MS2-C2 / DESIGN-MS-D3 §3 B2.

Closes the one corruption the dim-suffix does NOT catch: a *same-dim model swap* (the operational
768-dim embedder slot swapped for a different 768-dim model) writes healthy-looking but incompatible
vectors into the same vector space and silently breaks retrieval. An ``EmbedFingerprint`` = ``{model_id, dim,
normalize, distance, query_template_hash, doc_template_hash}`` is stamped in BOTH the federation record's ``meta.embed`` (source of truth)
and a reserved Qdrant sentinel point (independent drift detector) — DECISIONS-MS2 OD#6 = BOTH — and a
fail-closed assert refuses to read/write a mismatched space.

Purely additive + self-contained: imports only the stdlib + ``core.memory.models.SlotResolution``;
``qdrant_client`` is imported lazily inside the sentinel writers (a duck-typed client is passed in),
so the module imports with no qdrant installed. Does NOT edit ``federation.py`` (``meta`` round-trips
verbatim, no schema change) or ``embedder.py``. Consumed by C3 (read adapter) / C4 (write fence).
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import unicodedata
import uuid
from typing import Any, Dict, List, Optional

from core.memory.models import SlotResolution


# ---------------------------------------------------------------------------
# Exceptions (fail-closed)
# ---------------------------------------------------------------------------

class FingerprintError(RuntimeError):
    """Base error for embed fingerprint violations: malformed stamp, underspecified slot."""
    pass


class FingerprintMissing(FingerprintError):
    """Stamp absent where required (fail-closed)."""
    pass


class FingerprintMismatch(FingerprintError):
    """Registered fingerprint does not match the live fingerprint (fail-closed)."""

    def __init__(
        self,
        registered: "EmbedFingerprint",
        live: "EmbedFingerprint",
        fields: List[str],
        context: str = "",
    ) -> None:
        self.registered = registered
        self.live = live
        self.fields = fields
        self.context = context
        parts: List[str] = []
        for field in fields:
            reg_val = getattr(registered, field)
            live_val = getattr(live, field)
            parts.append(f"{field} (registered={reg_val!r} != live={live_val!r})")
        field_details = ", ".join(parts)
        ctx = f" for {context}" if context else ""
        message = f"embed fingerprint mismatch{ctx}: fields differ: {field_details}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# Frozen dataclass: EmbedFingerprint
# ---------------------------------------------------------------------------

TEMPLATE_ABSENT_SENTINEL = "template:absent:v1"
LEGACY_TEMPLATE_SENTINEL = "template:legacy-unknown:v1"


def _template_identity(template: str | None) -> str:
    """Return a stable identity for an optional instruction template."""
    if template is None:
        return TEMPLATE_ABSENT_SENTINEL
    if not isinstance(template, str):
        raise FingerprintError(
            f"instruction template must be str or None, got {type(template).__name__}"
        )
    return f"sha256:{hashlib.sha256(template.encode('utf-8')).hexdigest()}"


@dataclasses.dataclass(frozen=True)
class EmbedFingerprint:
    """Deterministic, hashable, comparable embed fingerprint (model_id, dim, normalize, distance).

    Validates and canonicalizes on creation (distance -> lowercase). Equality and hashing are
    value-based (two equal fingerprints compare equal and hash equal).
    """
    model_id: str
    dim: int
    normalize: bool
    distance: str
    query_template_hash: str = TEMPLATE_ABSENT_SENTINEL
    doc_template_hash: str = TEMPLATE_ABSENT_SENTINEL

    def __post_init__(self) -> None:
        # model_id: must be a non-empty string after strip
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise FingerprintError(
                f"model_id must be a non-empty string, got {self.model_id!r}"
            )
        # dim: must be a positive int, not bool
        if isinstance(self.dim, bool) or not isinstance(self.dim, int) or self.dim <= 0:
            raise FingerprintError(
                f"dim must be a positive integer (not bool), got {self.dim!r}"
            )
        # normalize: must be a bool
        if not isinstance(self.normalize, bool):
            raise FingerprintError(
                f"normalize must be a bool, got {self.normalize!r}"
            )
        # distance: non-empty string, then canonicalize to lowercase
        if not isinstance(self.distance, str) or not self.distance.strip():
            raise FingerprintError(
                f"distance must be a non-empty string, got {self.distance!r}"
            )
        for field in ("query_template_hash", "doc_template_hash"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise FingerprintError(
                    f"{field} must be a non-empty string, got {value!r}"
                )
        object.__setattr__(self, "distance", self.distance.strip().lower())
        object.__setattr__(
            self, "model_id", unicodedata.normalize("NFC", self.model_id.strip())
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-serialisable dict form (the meta.embed / sentinel-payload shape)."""
        return {
            "model_id": self.model_id,
            "dim": self.dim,
            "normalize": self.normalize,
            "distance": self.distance,
            "query_template_hash": self.query_template_hash,
            "doc_template_hash": self.doc_template_hash,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "EmbedFingerprint":
        """Construct from a template-aware dict; legacy four-field stamps become drift sentinels."""
        if not isinstance(d, dict):
            raise FingerprintError(
                f"EmbedFingerprint.from_dict expects a dict, got {type(d).__name__}"
            )
        for key in ("model_id", "dim", "normalize", "distance"):
            if key not in d:
                raise FingerprintError(f"Missing key {key!r} in fingerprint dict: {d}")
        model_id = d["model_id"]
        if not isinstance(model_id, str):
            raise FingerprintError(f"model_id must be str, got {type(model_id).__name__}")
        dim = d["dim"]
        if isinstance(dim, bool) or not isinstance(dim, int):
            raise FingerprintError(f"dim must be int (not bool), got {type(dim).__name__} {dim!r}")
        if dim <= 0:
            raise FingerprintError(f"dim must be positive, got {dim}")
        normalize = d["normalize"]
        if not isinstance(normalize, bool):
            raise FingerprintError(f"normalize must be bool, got {type(normalize).__name__}")
        distance = d["distance"]
        if not isinstance(distance, str):
            raise FingerprintError(f"distance must be str, got {type(distance).__name__}")
        template_keys = ("query_template_hash", "doc_template_hash")
        present_template_keys = [key for key in template_keys if key in d]
        if present_template_keys and len(present_template_keys) != len(template_keys):
            missing = [key for key in template_keys if key not in d]
            raise FingerprintError(f"Missing keys {missing!r} in fingerprint dict: {d}")
        if present_template_keys:
            query_template_hash = d["query_template_hash"]
            doc_template_hash = d["doc_template_hash"]
        else:
            query_template_hash = LEGACY_TEMPLATE_SENTINEL
            doc_template_hash = LEGACY_TEMPLATE_SENTINEL
        # Construct via cls(...) so __post_init__ re-validates + canonicalizes.
        return cls(
            model_id=model_id, dim=dim, normalize=normalize, distance=distance,
            query_template_hash=query_template_hash, doc_template_hash=doc_template_hash,
        )


# ---------------------------------------------------------------------------
# Derivation from a SlotResolution
# ---------------------------------------------------------------------------

def fingerprint_from_slot(
    resolution: SlotResolution,
    *,
    normalize: bool = True,
    distance: str = "cosine",
) -> EmbedFingerprint:
    """Derive an EmbedFingerprint from a SlotResolution; underspecified slot -> FingerprintError."""
    if not isinstance(resolution.model, str) or not resolution.model.strip():
        raise FingerprintError(
            f"Cannot fingerprint slot: model must be a non-empty string ({resolution.model!r})"
        )
    if resolution.embedding_dimension is None:
        raise FingerprintError(
            f"Cannot fingerprint slot: embedding_dimension is None for model {resolution.model!r}"
        )
    return EmbedFingerprint(
        model_id=resolution.model,
        dim=resolution.embedding_dimension,
        normalize=normalize,
        query_template_hash=_template_identity(
            getattr(resolution, "query_instruction_template", None)
        ),
        doc_template_hash=_template_identity(
            getattr(resolution, "doc_instruction_template", None)
        ),
        distance=distance,
    )


# ---------------------------------------------------------------------------
# Registry meta stamping (source of truth; federation.py is NOT edited)
# ---------------------------------------------------------------------------

def stamp_meta(
    meta: Optional[dict],
    fp: EmbedFingerprint,
    *,
    key: str = "embed",
) -> dict:
    """Return a NEW dict: deep-copy of *meta* (or {}) with result[key] = fp.to_dict(); never mutates."""
    result = copy.deepcopy(meta) if meta is not None else {}
    result[key] = fp.to_dict()
    return result


def read_meta_fingerprint(
    meta: Optional[dict],
    *,
    key: str = "embed",
) -> Optional[EmbedFingerprint]:
    """Read meta[key]; None if absent (legacy), FingerprintError if present-but-malformed."""
    if meta is None or key not in meta:
        return None
    return EmbedFingerprint.from_dict(meta[key])


# ---------------------------------------------------------------------------
# Fail-closed assertions (load-bearing — C3/C4 entry points)
# ---------------------------------------------------------------------------

def assert_compatible(
    registered: EmbedFingerprint,
    live: EmbedFingerprint,
    *,
    context: str = "",
) -> None:
    """Raise FingerprintMismatch iff a vector-space-affecting field differs."""
    fields: List[str] = []
    for field in (
        "model_id", "dim", "normalize", "distance",
        "query_template_hash", "doc_template_hash",
    ):
        if getattr(registered, field) != getattr(live, field):
            fields.append(field)
    if fields:
        raise FingerprintMismatch(registered, live, fields, context=context)


def assert_slot_matches_registry(
    meta: Optional[dict],
    resolution: SlotResolution,
    *,
    normalize: bool = True,
    distance: str = "cosine",
    require_stamp: bool = True,
    context: str = "",
) -> Optional[EmbedFingerprint]:
    """Compare the registered fingerprint (meta.embed) to the live slot fingerprint; fail-closed.

    Returns the registered fingerprint on a match; None only when require_stamp=False and the stamp is
    absent. Raises FingerprintMissing (absent stamp, required) or FingerprintMismatch (model/dim/etc).
    """
    registered = read_meta_fingerprint(meta)
    if registered is None:
        if require_stamp:
            ctx = f" for {context}" if context else ""
            raise FingerprintMissing(f"no embed fingerprint stamp in registry meta{ctx}")
        return None
    live = fingerprint_from_slot(resolution, normalize=normalize, distance=distance)
    assert_compatible(registered, live, context=context)
    return registered


# ---------------------------------------------------------------------------
# Qdrant sentinel (independent drift detector; "stamp on first write")
# ---------------------------------------------------------------------------

# A reserved namespace DISTINCT from the provider's chunk-id namespace
# (qdrant_provider._POINT_ID_NAMESPACE), so the sentinel id can never collide with a chunk id.
_SENTINEL_NAMESPACE = uuid.UUID("6f1b2c3d-0000-4000-8000-000000000c20")
SENTINEL_POINT_ID: str = str(uuid.uuid5(_SENTINEL_NAMESPACE, "bobclaw-embed-fingerprint-sentinel"))
SENTINEL_PAYLOAD_KEY = "_bobclaw_embed_fingerprint"
SENTINEL_MARKER_KEY = "_bobclaw_sentinel"


def sentinel_vector(dim: int) -> List[float]:
    """A deterministic, non-degenerate, unit-norm vector of length *dim* ([1.0, 0.0, ...])."""
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise FingerprintError(
            f"sentinel_vector requires a positive int for dim, got {dim!r}"
        )
    vec = [0.0] * dim
    vec[0] = 1.0
    return vec


def write_sentinel(client: Any, collection: str, fp: EmbedFingerprint) -> None:
    """Upsert the reserved sentinel point (id, vector, and fingerprint payload).

    Call only while the writer holds the collection family fence.
    """
    # Lazy import so the module imports without qdrant_client installed.
    from qdrant_client.http.models import PointStruct

    point = PointStruct(
        id=SENTINEL_POINT_ID,
        vector=sentinel_vector(fp.dim),
        payload={
            SENTINEL_PAYLOAD_KEY: fp.to_dict(),
            SENTINEL_MARKER_KEY: True,
        },
    )
    client.upsert(collection_name=collection, points=[point])


def read_sentinel(client: Any, collection: str) -> Optional[EmbedFingerprint]:
    """Retrieve the sentinel point's fingerprint; None if ABSENT, FingerprintError if present-but-malformed.

    The absent/malformed distinction is load-bearing and fail-closed: a genuinely missing sentinel
    (``retrieve`` returns no point) is ``None`` (legacy / pre-first-write — ``ensure_sentinel`` may then
    stamp it). But a point that EXISTS at the reserved sentinel id with a missing / non-dict / key-less
    payload is a corrupted or tampered sentinel — it raises ``FingerprintError`` so ``ensure_sentinel``
    refuses to silently overwrite it and ``assert_sentinel_matches`` does not mask real drift as
    ``FingerprintMissing``.
    """
    points = client.retrieve(
        collection_name=collection,
        ids=[SENTINEL_POINT_ID],
        with_payload=True,
    )
    if not points:
        return None  # genuinely absent (no point at the reserved id)
    point = points[0]
    # Support both record objects (with .payload) and raw dicts.
    payload = getattr(point, "payload", None)
    if payload is None and isinstance(point, dict):
        payload = point.get("payload")
    if not isinstance(payload, dict) or SENTINEL_PAYLOAD_KEY not in payload:
        raise FingerprintError(
            f"sentinel point {SENTINEL_POINT_ID!r} is present in collection {collection!r} but its "
            f"{SENTINEL_PAYLOAD_KEY!r} payload is missing/malformed (corrupted or tampered sentinel) — "
            "refusing to treat it as absent"
        )
    return EmbedFingerprint.from_dict(payload[SENTINEL_PAYLOAD_KEY])


def assert_sentinel_matches(
    client: Any,
    collection: str,
    fp: EmbedFingerprint,
    *,
    require_sentinel: bool = True,
) -> None:
    """Assert the live store's sentinel == *fp* (FingerprintMissing/Mismatch otherwise; fail-closed)."""
    stored = read_sentinel(client, collection)
    if stored is None:
        if require_sentinel:
            raise FingerprintMissing(
                f"no embed fingerprint sentinel in collection {collection!r}"
            )
        return
    assert_compatible(stored, fp, context=f"collection {collection!r}")


def ensure_sentinel(client: Any, collection: str, fp: EmbedFingerprint) -> None:
    """Write the sentinel if absent, otherwise assert it matches *fp*.

    Call only while the writer holds the collection family fence.
    """
    stored = read_sentinel(client, collection)
    if stored is None:
        write_sentinel(client, collection, fp)
    else:
        assert_compatible(stored, fp, context=f"collection {collection!r}")


# ---------------------------------------------------------------------------
# Zvec manifest sentinel (local equivalent of the Qdrant reserved point)
# ---------------------------------------------------------------------------

ZVEC_MANIFEST_FINGERPRINT_FILE = "embed_fingerprint.json"


def ensure_zvec_instance_fingerprint(
    manifest_dir: str | Path, fp: EmbedFingerprint
) -> Path:
    """Write a Zvec instance fingerprint if absent, otherwise assert compatibility.

    The manifest directory must already exist. Call only while the writer holds
    the collection family fence; bootstrap creates the layout and invokes this
    helper after ``WriteFence.assert_writable`` succeeds.
    """
    directory = Path(manifest_dir)
    if not directory.is_dir():
        raise FingerprintError(f"zvec fingerprint manifest directory is missing: {directory}")
    stamp_path = directory / ZVEC_MANIFEST_FINGERPRINT_FILE
    if stamp_path.exists():
        try:
            payload = json.loads(stamp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FingerprintError(
                f"zvec fingerprint manifest is unreadable or malformed: {stamp_path}"
            ) from exc
        if not isinstance(payload, dict) or "embed" not in payload:
            raise FingerprintError(
                f"zvec fingerprint manifest is missing its embed stamp: {stamp_path}"
            )
        stored = EmbedFingerprint.from_dict(payload["embed"])
        assert_compatible(stored, fp, context=f"zvec manifest {stamp_path}")
        return stamp_path

    payload = {"embed": fp.to_dict()}
    tmp_path = stamp_path.with_suffix(stamp_path.suffix + ".tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        tmp_path.replace(stamp_path)
    except OSError as exc:
        raise FingerprintError(
            f"could not write zvec fingerprint manifest {stamp_path}: {exc}"
        ) from exc
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    return stamp_path
