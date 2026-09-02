from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

from core.forest.events import ForestError   # READ-ONLY primitives

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
DEFAULT_COLLECTION = "research_forest"
TREE_ID_KEY = "tree_id"
PROJECTION_KEY_KEY = "projection_key"
KIND_KEY = "kind"
ITEM_ID_KEY = "item_id"
TEXT_KEY = "text"
ZERO_NORM_EPS = 1e-9
_POINT_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProjectionError(ForestError):
    """Raised on projection misuse or degenerate embedding."""
    pass

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def point_id(tree_id: str, kind: str, item_id: str) -> str:
    """Deterministic UUID5 (str) from f"{tree_id}\\x1f{kind}\\x1f{item_id}" under _POINT_NAMESPACE."""
    raw = f"{tree_id}\x1f{kind}\x1f{item_id}"
    return str(uuid.uuid5(_POINT_NAMESPACE, raw))

def deterministic_embedder(dim: int = 64) -> Callable[[List[str]], List[List[float]]]:
    """Return embed(texts)->vectors. Each text -> a REPRODUCIBLE non-zero float vector of length `dim`,
    derived by hashing (sha256 of f"{i}:{text}" chunks) and mapping bytes to floats in [-1,1], then
    L2-normalized to unit length. Empty/whitespace text still yields a fixed non-zero unit vector."""
    def _bytes_to_float(b: bytes) -> float:
        # interpret first 4 bytes as a signed integer, then scale to [-1,1]
        val = int.from_bytes(b[:4], byteorder='big', signed=True)
        return val / (2**31)   # approx [-1,1]

    def _text_vector(text: str) -> List[float]:
        # generate raw floats in [-1,1] for each dimension
        vec: List[float] = []
        for i in range(dim):
            h = hashlib.sha256(f"{i}:{text}".encode('utf-8')).digest()
            vec.append(_bytes_to_float(h))
        # L2 normalise; if zero, replace with fixed unit vector (first dim = 1.0)
        norm = math.sqrt(sum(x*x for x in vec))
        if norm < ZERO_NORM_EPS:
            # fixed non-zero unit vector
            return [1.0 if idx == 0 else 0.0 for idx in range(dim)]
        inv = 1.0 / norm
        return [x * inv for x in vec]

    def embed(texts: List[str]) -> List[List[float]]:
        return [_text_vector(t) for t in texts]

    return embed

def render_event_text(ev: dict) -> str:
    """Render event dict to text per kind rules."""
    kind = ev.get('kind', '')

    # Special handling for 'measurement'
    if kind == 'measurement':
        parts = ['measurement']
        for f in ('node_id', 'metric', 'value', 'source'):
            if f in ev:
                parts.append(f"{f}={ev[f]}")
        if 'unit' in ev:
            parts.append(f"unit={ev['unit']}")
        return ' '.join(parts)

    # Default: "<kind> <sorted key=value ...>"
    meta = {'id', 'kind', 'ts'}
    fields = [f"{k}={v}" for k, v in sorted(ev.items()) if k not in meta]
    return f"{kind} {' '.join(fields)}"

def render_claim_text(claim_id: str, claim: dict) -> str:
    """Render claim dict to text."""
    for key in ('statement', 'text', 'claim'):
        val = claim.get(key)
        if val is not None:
            return str(val)
    return json.dumps(claim, sort_keys=True)

# ---------------------------------------------------------------------------
# Projection class
# ---------------------------------------------------------------------------
class ForestProjection:
    """Manages a Qdrant collection for LKS projection of a program ledger."""

    def __init__(self, client, *, embedder, collection: str = DEFAULT_COLLECTION,
                 distance: str = "cosine") -> None:
        if not isinstance(collection, str) or not collection:
            raise ProjectionError("collection must be a non-empty string")
        self._client = client
        self._embedder = embedder
        self._collection = collection
        self._distance = distance  # stored but not used yet (reserved for future)

    # ---- embedding indirection ----
    def _embed(self, texts: List[str]) -> List[List[float]]:
        """Embed texts using injected embedder (supports callable or .embed method)."""
        if hasattr(self._embedder, 'embed') and callable(self._embedder.embed):
            return self._embedder.embed(texts)
        return self._embedder(texts)

    # ---- ledger -> point specs (pure) ----
    def project_points(self, tree_id: str, ledger: dict, projection_key: str) -> List[dict]:
        """Convert ledger dict to point specifications without embedding."""
        specs: List[dict] = []

        # claims sorted by id
        claims = ledger.get('claims', {})
        for claim_id in sorted(claims.keys()):
            claim = claims[claim_id]
            kind_str = 'claim'
            item_id = claim_id
            text = render_claim_text(claim_id, claim)
            specs.append({
                "id": point_id(tree_id, kind_str, item_id),
                "text": text,
                "payload": {
                    TREE_ID_KEY: tree_id,
                    PROJECTION_KEY_KEY: projection_key,
                    KIND_KEY: kind_str,
                    ITEM_ID_KEY: item_id,
                    TEXT_KEY: text,
                }
            })

        # events in ledger order
        events = ledger.get('events', [])
        for ev in events:
            ev_id = ev.get('id', '')
            if not ev_id:
                continue
            kind_str = 'event:' + str(ev.get('kind', ''))
            item_id = ev['id']
            text = render_event_text(ev)
            specs.append({
                "id": point_id(tree_id, kind_str, item_id),
                "text": text,
                "payload": {
                    TREE_ID_KEY: tree_id,
                    PROJECTION_KEY_KEY: projection_key,
                    KIND_KEY: kind_str,
                    ITEM_ID_KEY: item_id,
                    TEXT_KEY: text,
                }
            })

        return specs

    # ---- norm gate ----
    @staticmethod
    def vector_norm(vec: List[float]) -> float:
        """L2 norm of vector."""
        return math.sqrt(sum(x*x for x in vec))

    def assert_norms(self, vectors: List[List[float]]) -> None:
        """Raise ProjectionError if any vector is empty or has L2 norm <= ZERO_NORM_EPS."""
        for i, vec in enumerate(vectors):
            if not vec:
                raise ProjectionError(f"Empty vector at index {i}")
            if self.vector_norm(vec) <= ZERO_NORM_EPS:
                raise ProjectionError(f"Zero-norm vector at index {i}")

    # ---- collection lifecycle ----
    def ensure_collection(self, dim: int) -> None:
        """Create collection if not exists, using COSINE distance."""
        from qdrant_client.http.models import Distance, VectorParams

        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def drop_collection(self) -> None:
        """Full drop if collection exists."""
        if self._client.collection_exists(self._collection):
            self._client.delete_collection(collection_name=self._collection)

    def drop_tree(self, tree_id: str) -> None:
        """Delete only this tree's points (idempotent, no-op if no collection)."""
        from qdrant_client.http.models import (Filter, FieldCondition, MatchValue, FilterSelector)

        if not self._client.collection_exists(self._collection):
            return
        self._client.delete(
            collection_name=self._collection,
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key=TREE_ID_KEY, match=MatchValue(value=tree_id))
                ])
            ),
        )

    # ---- rebuild ----
    def rebuild(self, tree_id: str, ledger: dict, projection_key: str) -> dict:
        """Drop tree's old points and rebuild from ledger truth."""
        specs = self.project_points(tree_id, ledger, projection_key)
        texts = [s['text'] for s in specs]
        vectors = self._embed(texts) if specs else []

        # Norm gate BEFORE any mutation
        self.assert_norms(vectors)

        if vectors:
            dim = len(vectors[0])
            self.ensure_collection(dim)
        self.drop_tree(tree_id)

        if specs:
            from qdrant_client.http.models import PointStruct
            points = [
                PointStruct(id=s['id'], vector=v, payload=s['payload'])
                for s, v in zip(specs, vectors)
            ]
            self._client.upsert(collection_name=self._collection, points=points)

        return {
            "tree_id": tree_id,
            "projection_key": projection_key,
            "count": len(specs),
        }

    def rebuild_from_store(self, store, tree_id: Optional[str] = None) -> dict:
        """Rebuild using an F1 ProgramStore."""
        tree_id = tree_id or store.program_id
        ledger = store.read()
        pk = store.projection_key()
        return self.rebuild(tree_id, ledger, pk)

    # ---- freshness ----
    def stored_projection_key(self, tree_id: str) -> Optional[str]:
        """Get stored projection_key for a tree, or None if missing."""
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue

        if not self._client.collection_exists(self._collection):
            return None
        points, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=Filter(must=[
                FieldCondition(key=TREE_ID_KEY, match=MatchValue(value=tree_id))
            ]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        return points[0].payload.get(PROJECTION_KEY_KEY)

    def is_stale(self, tree_id: str, head_projection_key: str) -> bool:
        """True if stored projection_key does not match head."""
        stored = self.stored_projection_key(tree_id)
        return stored is None or stored != head_projection_key

    def freshness(self, store, tree_id: Optional[str] = None) -> dict:
        """Return freshness info for a tree."""
        tree_id = tree_id or store.program_id
        head = store.projection_key()
        stored = self.stored_projection_key(tree_id)
        stale = stored != head
        return {
            "tree_id": tree_id,
            "stored": stored,
            "head": head,
            "stale": stale,
        }

    # ---- retrieve ----
    def retrieve(self, *, query_text: Optional[str] = None, query_vector: Optional[List[float]] = None,
                 tree_id: Optional[str] = None, k: int = 5, collection: Optional[str] = None,
                 extra_filters: Optional[Dict[str, Union[str, List[str]]]] = None) -> list:
        """Query the collection for nearest neighbours."""
        if (query_text is None) == (query_vector is None):
            raise ProjectionError("Exactly one of query_text or query_vector must be given")
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ProjectionError("k must be a positive integer")

        coll = collection or self._collection

        if query_text is not None:
            vec = self._embed([query_text])[0]
        else:
            vec = list(query_vector)

        # Build filter
        from qdrant_client.http.models import (Filter, FieldCondition, MatchValue, MatchAny)
        conditions: List[FieldCondition] = []
        if tree_id is not None:
            conditions.append(FieldCondition(
                key=TREE_ID_KEY, match=MatchValue(value=tree_id)
            ))
        if extra_filters:
            for key, val in extra_filters.items():
                if isinstance(val, list):
                    conditions.append(FieldCondition(
                        key=key, match=MatchAny(any=val)
                    ))
                else:
                    conditions.append(FieldCondition(
                        key=key, match=MatchValue(value=val)
                    ))
        qfilter = Filter(must=conditions) if conditions else None

        if not self._client.collection_exists(coll):
            return []

        resp = self._client.query_points(
            collection_name=coll,
            query=vec,
            limit=k,
            query_filter=qfilter,
        )
        return [
            Hit(id=str(p.id), score=float(p.score), payload=(p.payload or {}))
            for p in resp.points[:k]
        ]

# ---------------------------------------------------------------------------
# Data model for retrieval results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    payload: dict

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__ = [
    "ProjectionError",
    "ForestProjection",
    "Hit",
    "DEFAULT_COLLECTION",
    "TREE_ID_KEY",
    "PROJECTION_KEY_KEY",
    "deterministic_embedder",
    "point_id",
    "render_event_text",
    "render_claim_text",
]
