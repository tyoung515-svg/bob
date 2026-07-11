from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from core.memory.acl import ACLRegistry
from core.memory.exceptions import RetrievalProviderError
from core.memory.models import (
    ChunkRecord,
    FilterExpr,
    HealthStatus,
    Hit,
    IndexReceipt,
    Query,
    RankedResults,
)
from core.memory.write_fence import is_collection_in_family

# Qdrant point IDs must be a uint64 or a UUID. Chunk ids are human-readable
# strings ("chunk:<fact_id>:<hash>"), so map them to a deterministic UUID5 —
# the same input always yields the same point id, so upsert and delete address
# the same point. The original ids live in the payload for downstream use.
_POINT_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _to_point_id(raw: Any) -> Any:
    if isinstance(raw, int):
        return raw
    try:
        return str(uuid.UUID(str(raw)))  # already UUID-shaped — keep as-is
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, str(raw)))

class QdrantRetrievalProvider:
    provider_id: str
    locality: Literal["local", "remote"]
    capability_classes: set[str]

    def __init__(
        self,
        provider_id: str,
        locality: Literal["local", "remote"],
        collection_prefix: str,
        acl_registry: ACLRegistry,
        client=None,
        qdrant_url: str | None = None,
        write_fence=None,
    ) -> None:
        self.provider_id = provider_id
        self.locality = locality
        self.collection_prefix = collection_prefix
        self._acl = acl_registry
        self.capability_classes = {"text_dense"}
        # MS2-C4 single-writer write fence (optional; default None ⇒ byte-identical legacy behavior).
        # When set, every upsert/delete target must be a strict member of the held family BEFORE the
        # mutating client call. Registry ACLs remain reserved for external corpora via lks_adapter.
        self._write_fence = write_fence

        if client is not None:
            self._client = client
        else:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=qdrant_url)

    def _enforce(self, store_id: str, capability_class: str) -> None:
        self._acl.enforce(
            store_id,
            self.provider_id,
            self.locality,
            capability_class,
        )

    def _collection_name(self, dim: int) -> str:
        return f"{self.collection_prefix}_{dim}"

    def index(self, store_id: str, items: list[ChunkRecord]) -> IndexReceipt:
        self._enforce(store_id, next(iter(self.capability_classes)))

        from qdrant_client.http.models import (
            Distance,
            PointStruct,
            VectorParams,
        )

        by_dim: dict[int, list[ChunkRecord]] = {}
        for item in items:
            by_dim.setdefault(len(item.vector), []).append(item)

        # MS2-C4 write fence: assert EVERY target collection is writable BEFORE any create/upsert, so a
        # multi-dim call can never PARTIALLY write (fail-closed atomicity — a non-family or unheld target
        # aborts the whole index with no mutation). None ⇒ legacy path is byte-identical below.
        if self._write_fence is not None:
            for dim in by_dim:
                self._write_fence.assert_writable(self._collection_name(dim))

        total_count = 0
        for dim, dim_items in by_dim.items():
            coll = self._collection_name(dim)
            try:
                self._client.get_collection(coll)
            except Exception:
                self._client.create_collection(
                    collection_name=coll,
                    vectors_config=VectorParams(
                        size=dim, distance=Distance.COSINE
                    ),
                )

            points = [
                PointStruct(
                    id=_to_point_id(item.id),
                    vector=item.vector,
                    payload={**item.payload, "chunk_id": item.id},
                )
                for item in dim_items
            ]
            self._client.upsert(collection_name=coll, points=points)
            total_count += len(points)

        return IndexReceipt(
            provider_id=self.provider_id,
            store_id=store_id,
            item_count=total_count,
            ts=_now(),
        )

    def query(
        self,
        store_id: str,
        q: Query,
        k: int,
        filters: FilterExpr | None,
    ) -> RankedResults:
        raise RetrievalProviderError(
            self.provider_id,
            "query(Query) requires text→vector embedding at the provider edge, "
            "deferred to Phase 2. Use query_vector(store_id, vector, k, filters) "
            "with a pre-embedded vector instead.",
        )

    def query_vector(
        self,
        store_id: str,
        vector: list[float],
        k: int = 10,
        filters: FilterExpr | None = None,
    ) -> RankedResults:
        self._enforce(store_id, next(iter(self.capability_classes)))

        from qdrant_client.http.models import Filter as QdrantFilter

        dim = len(vector)
        coll = self._collection_name(dim)

        # A missing collection means nothing has been indexed for this dim yet —
        # that's "no matches", not an error. Returning empty here (instead of
        # letting query_points 404) lets recall run on a fresh deployment before
        # the first fact is seeded. Genuine errors below still propagate.
        try:
            if not self._client.collection_exists(coll):
                return RankedResults(
                    hits=[], provider_id=self.provider_id, latency_ms=0
                )
        except Exception:
            # collection_exists itself failing is a real connectivity problem —
            # fall through so query_points raises a descriptive error.
            pass

        qdrant_filter: QdrantFilter | None = None
        if filters:
            qdrant_filter = _build_filter(filters)

        t0 = time.monotonic()
        try:
            # qdrant-client >=1.12 removed .search(); query_points() is the
            # replacement and returns a response whose .points are ScoredPoints
            # with the same .id/.score/.payload shape.
            results = self._client.query_points(
                collection_name=coll,
                query=vector,
                limit=k,
                query_filter=qdrant_filter,
            ).points
        except Exception as exc:
            raise RetrievalProviderError(
                self.provider_id,
                f"search failed on collection {coll!r}: {exc}",
            ) from exc

        latency_ms = int((time.monotonic() - t0) * 1000)

        hits = [
            Hit(id=str(r.id), score=float(r.score), payload=r.payload or {})
            for r in results
        ]
        hits.sort(key=lambda h: h.score, reverse=True)

        return RankedResults(
            hits=hits,
            provider_id=self.provider_id,
            latency_ms=latency_ms,
        )

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        from qdrant_client.http.models import PointIdsList

        try:
            collections = self._client.get_collections().collections
        except Exception as exc:
            raise RetrievalProviderError(
                self.provider_id,
                f"failed to list collections for delete: {exc}",
            ) from exc

        targets = [
            ci.name for ci in collections
            if is_collection_in_family(ci.name, self.collection_prefix)
        ]
        # MS2-C4 write fence: assert EVERY matching collection is writable BEFORE any delete, so a refused
        # collection aborts the whole delete with no PARTIAL mutation. Strict selection makes the target
        # set a subset of fence authorization; None keeps the legacy call flow below.
        if self._write_fence is not None:
            for coll_name in targets:
                self._write_fence.assert_writable(coll_name)

        errors: list[str] = []
        for coll_name in targets:
            try:
                self._client.delete(
                    collection_name=coll_name,
                    points_selector=PointIdsList(
                        points=[_to_point_id(i) for i in item_ids]
                    ),
                )
            except Exception as exc:
                errors.append(f"{coll_name}: {exc}")

        if errors:
            raise RetrievalProviderError(
                self.provider_id,
                f"delete failed on collections: {'; '.join(errors)}",
            )

    def scroll_payload(
        self,
        store_id: str,
        payload_filter: dict,
        batch_size: int = 128,
    ):
        self._enforce(store_id, next(iter(self.capability_classes)))

        from qdrant_client.http.models import (
            FieldCondition,
            Filter as QdrantFilter,
            MatchValue,
        )

        conditions = [
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in payload_filter.items()
        ]
        qdrant_filter = QdrantFilter(must=conditions) if conditions else None

        try:
            collections = self._client.get_collections().collections
        except Exception as exc:
            raise RetrievalProviderError(
                self.provider_id,
                f"failed to list collections for scroll: {exc}",
            ) from exc

        for coll_info in collections:
            coll_name = coll_info.name
            if not is_collection_in_family(coll_name, self.collection_prefix):
                continue
            next_offset = None
            while True:
                try:
                    points, next_offset = self._client.scroll(
                        collection_name=coll_name,
                        scroll_filter=qdrant_filter,
                        limit=batch_size,
                        with_payload=False,
                        with_vectors=False,
                        offset=next_offset,
                    )
                except Exception as exc:
                    raise RetrievalProviderError(
                        self.provider_id,
                        f"scroll failed on collection {coll_name!r}: {exc}",
                    ) from exc
                for point in points:
                    yield str(point.id)
                if next_offset is None:
                    break

    def health(self) -> HealthStatus:
        try:
            self._client.get_collections()
            return HealthStatus(ok=True)
        except Exception as exc:
            return HealthStatus(ok=False, detail=str(exc))


def _build_filter(filters: dict) -> Any:
    from qdrant_client.http.models import (
        FieldCondition,
        Filter as QdrantFilter,
        MatchValue,
    )

    conditions = [
        FieldCondition(key=key, match=MatchValue(value=value))
        for key, value in filters.items()
    ]
    if not conditions:
        return None
    return QdrantFilter(must=conditions)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
