from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
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
    RankedResults,
)
from core.memory.write_fence import is_collection_in_family

_POINT_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_RECLAIM_BACKOFF_S = 0.25
_RECLAIM_TIMEOUT_S = 10.0
_VECTOR_FIELD = "embedding"
_SOURCE_FIELD = "source_fact_id"
_CHUNK_FIELD = "chunk_id"
_PAYLOAD_FIELD = "payload_json"


def _to_point_id(raw: Any) -> Any:
    if isinstance(raw, int):
        return raw
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, str(raw)))
class _WorkerOperationError(Exception):
    def __init__(self, error_type: str, detail: str) -> None:
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"{error_type}: {detail}")


class ZvecRetrievalProvider:
    """A synchronous RetrievalProvider backed by one subprocess-owned Zvec worker."""

    provider_id: str
    locality: Literal["local", "remote"]
    capability_classes: set[str]

    def __init__(
        self,
        provider_id: str,
        locality: Literal["local", "remote"],
        collection_prefix: str,
        acl_registry: ACLRegistry,
        store_root: str | Path,
        write_fence=None,
        *,
        python_executable: str | None = None,
        reclaim_timeout_s: float = _RECLAIM_TIMEOUT_S,
    ) -> None:
        if not isinstance(reclaim_timeout_s, (int, float)) or reclaim_timeout_s <= 0:
            raise ValueError("reclaim_timeout_s must be positive")
        self.provider_id = provider_id
        self.locality = locality
        self.collection_prefix = collection_prefix
        self._acl = acl_registry
        self.capability_classes = {"text_dense"}
        self._store_root = Path(store_root).expanduser().resolve()
        self._write_fence = write_fence
        self._python_executable = python_executable or sys.executable
        self._reclaim_timeout_s = float(reclaim_timeout_s)
        self._child: subprocess.Popen[str] | None = None
        self._ipc_lock = threading.RLock()
        self._last_error = ""
        self._start_child()

    def _enforce(self, store_id: str, capability_class: str) -> None:
        self._acl.enforce(store_id, self.provider_id, self.locality, capability_class)

    def _collection_name(self, dim: int) -> str:
        return f"{self.collection_prefix}_{dim}"

    def _instance_dir(self, store_id: str) -> Path:
        if not isinstance(store_id, str) or not store_id.strip():
            raise RetrievalProviderError(self.provider_id, "store_id must be a non-empty string")
        candidate = Path(store_id)
        if candidate.name != store_id or candidate.is_absolute() or store_id in {".", ".."}:
            raise RetrievalProviderError(self.provider_id, f"unsafe store_id {store_id!r}")
        return self._store_root / "instances" / store_id

    def _collections_dir(self, store_id: str) -> Path:
        return self._instance_dir(store_id) / "collections"

    def _collection_dir(self, store_id: str, collection: str) -> Path:
        return self._collections_dir(store_id) / collection

    def _family_collections(self, store_id: str) -> list[tuple[str, Path, int]]:
        root = self._collections_dir(store_id)
        if not root.is_dir():
            return []
        collections: list[tuple[str, Path, int]] = []
        for child in root.iterdir():
            if not child.is_dir() or not is_collection_in_family(child.name, self.collection_prefix):
                continue
            collections.append((child.name, child, int(child.name.rsplit("_", 1)[1])))
        return sorted(collections, key=lambda item: item[0])

    def _start_child(self) -> subprocess.Popen[str]:
        module_root = str(Path(__file__).resolve().parents[3])
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (module_root, env.get("PYTHONPATH")) if part
        )
        try:
            child = subprocess.Popen(
                [self._python_executable, "-m", "core.memory.providers.zvec_provider", "--worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise RetrievalProviderError(
                self.provider_id, f"could not start Zvec storage child: {exc}"
            ) from exc
        self._child = child
        return child

    def _discard_child(self, *, kill: bool) -> None:
        child = self._child
        self._child = None
        if child is None:
            return
        try:
            if kill and child.poll() is None:
                child.kill()
            if child.poll() is None:
                child.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            if child.poll() is None:
                try:
                    child.kill()
                    child.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _request(self, operation: str, **payload: Any) -> dict[str, Any]:
        with self._ipc_lock:
            child = self._child
            if child is None:
                child = self._start_child()
            if child.poll() is not None:
                code = child.returncode
                self._discard_child(kill=False)
                raise RetrievalProviderError(
                    self.provider_id,
                    f"Zvec storage child exited before {operation} (exit code {code})",
                )
            assert child.stdin is not None
            assert child.stdout is not None
            try:
                child.stdin.write(json.dumps({"op": operation, **payload}) + "\n")
                child.stdin.flush()
                line = child.stdout.readline()
            except (OSError, ValueError, BrokenPipeError) as exc:
                self._discard_child(kill=False)
                raise RetrievalProviderError(
                    self.provider_id, f"Zvec storage child unavailable during {operation}: {exc}"
                ) from exc
            if not line:
                code = child.poll()
                self._discard_child(kill=False)
                raise RetrievalProviderError(
                    self.provider_id,
                    f"Zvec storage child unavailable during {operation} (exit code {code})",
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self._discard_child(kill=True)
                raise RetrievalProviderError(
                    self.provider_id, f"invalid Zvec storage child response during {operation}"
                ) from exc
            if response.get("ok"):
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RetrievalProviderError(
                        self.provider_id,
                        f"invalid Zvec storage child result during {operation}",
                    )
                return result
            error = response.get("error") or {}
            raise _WorkerOperationError(
                str(error.get("type", "ZvecWorkerError")),
                str(error.get("detail", "unknown storage child error")),
            )

    @staticmethod
    def _is_lock_error(error: _WorkerOperationError) -> bool:
        return "can\'t lock" in error.detail.lower() or "cannot lock" in error.detail.lower()

    def _call_with_reclaim(self, operation: str, **payload: Any) -> dict[str, Any]:
        deadline = time.monotonic() + self._reclaim_timeout_s
        while True:
            try:
                result = self._request(operation, **payload)
            except _WorkerOperationError as exc:
                detail = f"{exc.error_type}: {exc.detail}"
                self._last_error = detail
                if not self._is_lock_error(exc):
                    raise RetrievalProviderError(
                        self.provider_id, f"{operation} failed: {detail}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise RetrievalProviderError(
                        self.provider_id,
                        f"Zvec collection reclaim timed out after {self._reclaim_timeout_s:.1f}s: {detail}",
                    ) from exc
                time.sleep(_RECLAIM_BACKOFF_S)
                continue
            self._last_error = ""
            return result

    def index(self, store_id: str, items: list[ChunkRecord]) -> IndexReceipt:
        self._enforce(store_id, next(iter(self.capability_classes)))
        by_dim: dict[int, list[ChunkRecord]] = {}
        for item in items:
            by_dim.setdefault(len(item.vector), []).append(item)

        if self._write_fence is not None:
            for dim in by_dim:
                self._write_fence.assert_writable(self._collection_name(dim))

        if by_dim:
            batches = [
                {
                    "collection": self._collection_name(dim),
                    "path": str(self._collection_dir(store_id, self._collection_name(dim))),
                    "dim": dim,
                    "items": [
                        {"id": item.id, "vector": item.vector, "payload": item.payload}
                        for item in dim_items
                    ],
                }
                for dim, dim_items in by_dim.items()
            ]
            self._call_with_reclaim("index", batches=batches)

        return IndexReceipt(
            provider_id=self.provider_id,
            store_id=store_id,
            item_count=len(items),
            ts=_now(),
        )

    def query_vector(
        self,
        store_id: str,
        vector: list[float],
        k: int = 10,
        filters: FilterExpr | None = None,
    ) -> RankedResults:
        self._enforce(store_id, next(iter(self.capability_classes)))
        collection = self._collection_name(len(vector))
        path = self._collection_dir(store_id, collection)
        if not path.is_dir():
            return RankedResults(hits=[], provider_id=self.provider_id, latency_ms=0)

        source_fact_id = _source_filter_value(filters)
        t0 = time.monotonic()
        response = self._call_with_reclaim(
            "query",
            path=str(path),
            vector=vector,
            k=k,
            source_fact_id=source_fact_id,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        hits = [
            Hit(
                id=str(item["id"]),
                score=float(item["score"]),
                payload=dict(item.get("payload") or {}),
            )
            for item in response.get("hits", [])
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return RankedResults(hits=hits, provider_id=self.provider_id, latency_ms=latency_ms)

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        targets = self._family_collections(store_id)
        if self._write_fence is not None:
            for collection, _, _ in targets:
                self._write_fence.assert_writable(collection)
        if not targets or not item_ids:
            return
        self._call_with_reclaim(
            "delete",
            batches=[
                {"path": str(path), "point_ids": [str(_to_point_id(item_id)) for item_id in item_ids]}
                for _, path, _ in targets
            ],
        )

    def scroll_payload(
        self,
        store_id: str,
        payload_filter: dict,
        batch_size: int = 128,
    ):
        self._enforce(store_id, next(iter(self.capability_classes)))
        source_fact_id = _source_filter_value(payload_filter)
        targets = self._family_collections(store_id)
        if not targets:
            return iter(())
        response = self._call_with_reclaim(
            "scroll",
            source_fact_id=source_fact_id,
            batch_size=batch_size,
            paths=[str(path) for _, path, _ in targets],
        )
        return iter(str(point_id) for point_id in response.get("ids", []))

    def health(self) -> HealthStatus:
        if self._last_error:
            return HealthStatus(ok=False, detail=self._last_error)
        try:
            self._request("ping")
            return HealthStatus(ok=True)
        except (RetrievalProviderError, _WorkerOperationError) as exc:
            return HealthStatus(ok=False, detail=str(exc))

    def close(self) -> None:
        with self._ipc_lock:
            child = self._child
            if child is None:
                return
            if child.poll() is None:
                try:
                    self._request("shutdown")
                except (RetrievalProviderError, _WorkerOperationError):
                    self._discard_child(kill=True)
                    return
            self._discard_child(kill=False)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _source_filter_value(filters: FilterExpr | None) -> str | None:
    if not filters:
        return None
    if set(filters) != {_SOURCE_FIELD}:
        raise RetrievalProviderError(
            "zvec",
            "Zvec supports filters only on declared source_fact_id",
        )
    value = filters[_SOURCE_FIELD]
    if not isinstance(value, str):
        raise RetrievalProviderError("zvec", "source_fact_id filter must be a string")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_main() -> int:
    import zvec

    collections: dict[str, Any] = {}
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = request["op"]
            if operation == "shutdown":
                collections.clear()
                gc.collect()
                _write_response({"ok": True, "result": {}})
                return 0
            result = _worker_dispatch(zvec, collections, operation, request)
            _write_response({"ok": True, "result": result})
        except Exception as exc:
            _write_response(
                {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "detail": str(exc)},
                }
            )
    collections.clear()
    gc.collect()
    return 0


def _write_response(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _worker_dispatch(zvec, collections: dict[str, Any], operation: str, request: dict[str, Any]):
    if operation == "ping":
        return {}
    if operation == "index":
        for batch in request["batches"]:
            collection = _worker_collection(
                zvec,
                collections,
                path=batch["path"],
                collection_name=batch["collection"],
                dim=int(batch["dim"]),
                create=True,
            )
            docs = []
            for item in batch["items"]:
                payload = dict(item["payload"])
                source_fact_id = payload.pop(_SOURCE_FIELD, "")
                payload.pop(_CHUNK_FIELD, None)
                if source_fact_id is None:
                    source_fact_id = ""
                if not isinstance(source_fact_id, str):
                    raise ValueError("source_fact_id must be a string when supplied")
                docs.append(
                    zvec.Doc(
                        id=str(_to_point_id(item["id"])),
                        vectors={_VECTOR_FIELD: item["vector"]},
                        fields={
                            _SOURCE_FIELD: source_fact_id,
                            _CHUNK_FIELD: item["id"],
                            _PAYLOAD_FIELD: json.dumps(payload, separators=(",", ":")),
                        },
                    )
                )
            _assert_success(collection.upsert(docs))
            collection.flush()
        return {}
    if operation == "query":
        collection = _worker_collection(
            zvec,
            collections,
            path=request["path"],
            collection_name="",
            dim=0,
            create=False,
        )
        docs = collection.query(
            zvec.Query(field_name=_VECTOR_FIELD, vector=request["vector"]),
            topk=int(request["k"]),
            filter=_zvec_filter(request.get("source_fact_id")),
            output_fields=[_SOURCE_FIELD, _CHUNK_FIELD, _PAYLOAD_FIELD],
        )
        return {"hits": [_worker_doc_result(doc) for doc in docs]}
    if operation == "delete":
        for batch in request["batches"]:
            collection = _worker_collection(
                zvec,
                collections,
                path=batch["path"],
                collection_name="",
                dim=0,
                create=False,
            )
            existing = collection.fetch(batch["point_ids"], include_vector=False)
            if existing:
                _assert_success(collection.delete(list(existing)))
                collection.flush()
        return {}
    if operation == "scroll":
        ids: list[str] = []
        for path in request["paths"]:
            collection = _worker_collection(
                zvec,
                collections,
                path=path,
                collection_name="",
                dim=0,
                create=False,
            )
            doc_count = int(collection.stats.doc_count)
            if doc_count == 0:
                continue
            docs = collection.query(
                topk=max(int(request["batch_size"]), doc_count),
                filter=_zvec_filter(request["source_fact_id"]),
                output_fields=[_CHUNK_FIELD],
            )
            ids.extend(str(doc.id) for doc in docs)
        return {"ids": ids}
    raise ValueError(f"unknown operation {operation!r}")


def _worker_collection(
    zvec,
    collections: dict[str, Any],
    *,
    path: str,
    collection_name: str,
    dim: int,
    create: bool,
):
    collection = collections.get(path)
    if collection is not None:
        return collection
    collection_path = Path(path)
    if create and not collection_path.exists():
        collection_path.parent.mkdir(parents=True, exist_ok=True)
        schema = zvec.CollectionSchema(
            name=collection_name,
            vectors=[
                zvec.VectorSchema(
                    _VECTOR_FIELD,
                    zvec.DataType.VECTOR_FP32,
                    dimension=dim,
                )
            ],
            fields=[
                zvec.FieldSchema(_SOURCE_FIELD, zvec.DataType.STRING),
                zvec.FieldSchema(_CHUNK_FIELD, zvec.DataType.STRING),
                zvec.FieldSchema(_PAYLOAD_FIELD, zvec.DataType.STRING),
            ],
        )
        collection = zvec.create_and_open(str(collection_path), schema)
    else:
        collection = zvec.open(str(collection_path))
    collections[path] = collection
    return collection


def _assert_success(statuses: Any) -> None:
    iterable = statuses if isinstance(statuses, list) else [statuses]
    failures = []
    for status in iterable:
        code = getattr(status, "code", 0)
        if callable(code):
            code = code()
        code = getattr(code, "value", code)
        if code != 0:
            failures.append(str(status))
    if failures:
        raise RuntimeError("Zvec mutation failed: " + "; ".join(failures))


def _zvec_filter(source_fact_id: str | None) -> str | None:
    if source_fact_id is None:
        return None
    escaped = source_fact_id.replace("\\", "\\\\").replace("'", "\\'")
    return f"{_SOURCE_FIELD} = '{escaped}'"


def _worker_doc_result(doc) -> dict[str, Any]:
    fields = dict(doc.fields or {})
    payload_raw = fields.get(_PAYLOAD_FIELD, "{}")
    payload = json.loads(payload_raw)
    source_fact_id = fields.get(_SOURCE_FIELD)
    if source_fact_id:
        payload[_SOURCE_FIELD] = source_fact_id
    payload[_CHUNK_FIELD] = fields.get(_CHUNK_FIELD, "")
    return {"id": str(doc.id), "score": float(doc.score or 0.0), "payload": payload}


if __name__ == "__main__":
    if "--worker" not in sys.argv:
        raise SystemExit("zvec_provider is a library module")
    raise SystemExit(_worker_main())



