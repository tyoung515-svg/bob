"""Subprocess-owned Zvec retrieval provider.

Supported provider filters are equality on ``source_fact_id`` only. Production
uses that key in ``MemoryIndexer.drop_facts`` via ``scroll_payload``; the live
recall path currently issues unfiltered vector searches. ``include_deprecated``
is consumed by ``MemoryRetriever`` before provider dispatch. Any other provider
filter fails closed so production call-site drift is visible.

The canonical storage encoding is one declared ``source_fact_id_state`` field
(``missing``, ``none``, or ``value``) plus the raw declared ``source_fact_id``
string. ``payload_json`` stores only the remaining payload. Pre-release Zvec
dev stores from earlier intermediate encodings have no upgrade contract: they
are derived data and must be dropped and re-ingested before first release.
Every index request is fully validated before its first upsert; a process crash
or Zvec failure during later write batches can still leave a partial prefix
because Zvec 0.5.1 has no transaction spanning batches.
"""

from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, TextIO

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
_REQUEST_TIMEOUT_S = 10.0
_MAX_UPSERT_BATCH = 1024
_VECTOR_FIELD = "embedding"
_SOURCE_FIELD = "source_fact_id"
_CHUNK_FIELD = "chunk_id"
_PAYLOAD_FIELD = "payload_json"
_SOURCE_STATE_FIELD = "source_fact_id_state"
_SOURCE_STATE_MISSING = "missing"
_SOURCE_STATE_NONE = "none"
_SOURCE_STATE_VALUE = "value"
_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _to_point_id(raw: Any) -> Any:
    if isinstance(raw, int):
        return raw
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, str(raw)))


class _WorkerOperationError(Exception):
    def __init__(self, error_type: str, detail: str, context: dict[str, Any] | None = None) -> None:
        self.error_type = error_type
        self.detail = detail
        self.context = context or {}
        super().__init__(f"{error_type}: {detail}")


class ZvecRetrievalProvider:
    """Synchronous retrieval provider backed by one owned Zvec subprocess."""

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
        request_timeout_s: float = _REQUEST_TIMEOUT_S,
        worker_command: list[str] | None = None,
    ) -> None:
        _validate_collection_prefix(collection_prefix)
        if not isinstance(reclaim_timeout_s, (int, float)) or reclaim_timeout_s <= 0:
            raise ValueError("reclaim_timeout_s must be positive")
        if not isinstance(request_timeout_s, (int, float)) or request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        if worker_command is not None and (
            not isinstance(worker_command, list)
            or not worker_command
            or not all(isinstance(part, str) and part for part in worker_command)
        ):
            raise ValueError("worker_command must be a non-empty list of strings")

        self.provider_id = provider_id
        self.locality = locality
        self.collection_prefix = collection_prefix
        self._acl = acl_registry
        self.capability_classes = {"text_dense"}
        self._store_root = Path(store_root).expanduser().resolve()
        self._write_fence = write_fence
        self._python_executable = python_executable or sys.executable
        self._worker_command = list(worker_command) if worker_command is not None else None
        self._reclaim_timeout_s = float(reclaim_timeout_s)
        self._request_timeout_s = float(request_timeout_s)
        self._child: subprocess.Popen[str] | None = None
        self._ipc_lock = threading.RLock()
        self._response_queue: queue.Queue[tuple[str, Any]] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_handle: TextIO | None = None
        self._last_error = ""
        self._degraded_paths: list[str] = []
        self._last_reclaim_retries = 0
        self._last_stream_page_sizes: list[int] = []
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
            if (
                not child.is_dir()
                or not is_collection_in_family(child.name, self.collection_prefix)
            ):
                continue
            collections.append((child.name, child, int(child.name.rsplit("_", 1)[1])))
        return sorted(collections, key=lambda item: item[0])

    def _start_child(self) -> subprocess.Popen[str]:
        module_root = str(Path(__file__).resolve().parents[3])
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (module_root, env.get("PYTHONPATH")) if part
        )
        command = self._worker_command or [
            self._python_executable,
            "-u",
            "-m",
            "core.memory.providers.zvec_provider",
            "--worker",
        ]
        log_dir = self._store_root / "_ipc"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"child-{uuid.uuid4().hex}.stderr.log"
        try:
            stderr_handle = log_path.open("w", encoding="utf-8")
            child = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            try:
                stderr_handle.close()
            except UnboundLocalError:
                pass
            raise RetrievalProviderError(
                self.provider_id, f"could not start Zvec storage child: {exc}"
            ) from exc

        assert child.stdout is not None
        assert child.stderr is not None
        response_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(child, child.stdout, response_queue),
            daemon=True,
            name=f"zvec-stdout-{child.pid}",
        )
        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(child.stderr, stderr_handle),
            daemon=True,
            name=f"zvec-stderr-{child.pid}",
        )
        self._child = child
        self._response_queue = response_queue
        self._stdout_thread = stdout_thread
        self._stderr_thread = stderr_thread
        self._stderr_handle = stderr_handle
        stdout_thread.start()
        stderr_thread.start()
        if self._worker_command is None:
            deadline = time.monotonic() + self._request_timeout_s
            try:
                self._send_request("ping", {})
                self._parse_single_response("startup", self._read_frame("startup", deadline))
            except (_WorkerOperationError, RetrievalProviderError) as exc:
                self._discard_child(kill=True)
                raise RetrievalProviderError(
                    self.provider_id, f"Zvec storage child failed startup handshake: {exc}"
                ) from exc
        return child

    @staticmethod
    def _drain_stdout(
        child: subprocess.Popen[str],
        stream: TextIO,
        response_queue: queue.Queue[tuple[str, Any]],
    ) -> None:
        try:
            for line in stream:
                response_queue.put(("line", line))
        except (OSError, ValueError) as exc:
            response_queue.put(("error", str(exc)))
        finally:
            response_queue.put(("eof", child.poll()))

    @staticmethod
    def _drain_stderr(stream: TextIO, log_handle: TextIO) -> None:
        try:
            raw_stream = stream.buffer
            while True:
                chunk = raw_stream.read1(65536)
                if not chunk:
                    return
                log_handle.write(chunk.decode("utf-8", errors="replace"))
                log_handle.flush()
        except (OSError, ValueError):
            return
    def _discard_child(self, *, kill: bool) -> None:
        child = self._child
        stdout_thread = self._stdout_thread
        stderr_thread = self._stderr_thread
        stderr_handle = self._stderr_handle
        self._child = None
        self._response_queue = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._stderr_handle = None
        if child is not None:
            try:
                if kill and child.poll() is None:
                    child.kill()
                if child.poll() is None:
                    child.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                if child.poll() is None:
                    try:
                        child.kill()
                        child.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            try:
                if child.stdin is not None:
                    child.stdin.close()
            except OSError:
                pass
            if stdout_thread is not None:
                stdout_thread.join(timeout=0.1)
            if stderr_thread is not None:
                stderr_thread.join(timeout=0.1)
            if stdout_thread is None or not stdout_thread.is_alive():
                try:
                    if child.stdout is not None:
                        child.stdout.close()
                except OSError:
                    pass
            if stderr_thread is None or not stderr_thread.is_alive():
                try:
                    if child.stderr is not None:
                        child.stderr.close()
                except OSError:
                    pass
        if stderr_handle is not None:
            try:
                stderr_handle.close()
            except OSError:
                pass
    def _malformed_response(self, operation: str, detail: str) -> None:
        self._discard_child(kill=True)
        raise RetrievalProviderError(
            self.provider_id,
            f"malformed Zvec storage child response during {operation}: {detail}",
        )

    def _read_frame(self, operation: str, deadline: float) -> Any:
        response_queue = self._response_queue
        if response_queue is None:
            raise RetrievalProviderError(
                self.provider_id, f"Zvec storage child unavailable during {operation}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._discard_child(kill=True)
            raise RetrievalProviderError(
                self.provider_id, f"Zvec storage child timed out during {operation}"
            )
        try:
            kind, value = response_queue.get(timeout=remaining)
        except queue.Empty as exc:
            self._discard_child(kill=True)
            raise RetrievalProviderError(
                self.provider_id, f"Zvec storage child timed out during {operation}"
            ) from exc
        if kind == "error":
            self._discard_child(kill=True)
            raise RetrievalProviderError(
                self.provider_id,
                f"Zvec storage child stdout failed during {operation}: {value}",
            )
        if kind == "eof":
            code = value
            child = self._child
            if child is not None:
                try:
                    code = child.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    code = child.poll()
            self._discard_child(kill=False)
            raise RetrievalProviderError(
                self.provider_id,
                f"Zvec storage child unavailable during {operation} (exit code {code})",
            )
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            self._malformed_response(operation, "invalid JSON")
            raise AssertionError from exc

    def _send_request(self, operation: str, payload: dict[str, Any]) -> None:
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
        try:
            child.stdin.write(json.dumps({"op": operation, **payload}) + "\n")
            child.stdin.flush()
        except (OSError, ValueError, BrokenPipeError) as exc:
            self._discard_child(kill=False)
            raise RetrievalProviderError(
                self.provider_id,
                f"Zvec storage child unavailable during {operation}: {exc}",
            ) from exc

    def _parse_single_response(self, operation: str, response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            self._malformed_response(operation, "top-level value is not an object")
        if response.get("ok") is True:
            if set(response) != {"ok", "result"} or not isinstance(response["result"], dict):
                self._malformed_response(operation, "success frame requires object result")
            return response["result"]
        if response.get("ok") is False:
            error = response.get("error")
            if set(response) != {"ok", "error"} or not isinstance(error, dict):
                self._malformed_response(operation, "error frame requires error object")
            if set(error) != {"type", "detail"}:
                self._malformed_response(operation, "error object requires type and detail")
            error_type = error["type"]
            detail = error["detail"]
            if (
                not isinstance(error_type, str)
                or not error_type
                or not isinstance(detail, str)
                or not detail
            ):
                self._malformed_response(
                    operation, "error type and detail must be non-empty strings"
                )
            raise _WorkerOperationError(error_type, detail)
        self._malformed_response(operation, "ok must be exactly true or false")
        raise AssertionError

    def _request(
        self,
        operation: str,
        *,
        deadline: float | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        with self._ipc_lock:
            request_deadline = deadline or (time.monotonic() + self._request_timeout_s)
            self._send_request(operation, payload)
            return self._parse_single_response(
                operation, self._read_frame(operation, request_deadline)
            )

    def _request_pages(
        self,
        operation: str,
        *,
        deadline: float,
        **payload: Any,
    ) -> list[list[str]]:
        with self._ipc_lock:
            self._send_request(operation, payload)
            pages: list[list[str]] = []
            while True:
                response = self._read_frame(operation, deadline)
                if not isinstance(response, dict):
                    self._malformed_response(operation, "top-level value is not an object")
                if response.get("ok") is False:
                    self._parse_single_response(operation, response)
                    raise AssertionError
                if response.get("ok") is not True or set(response) != {"ok", "page", "done"}:
                    self._malformed_response(operation, "stream frame requires ok, page, and done")
                page = response["page"]
                done = response["done"]
                if (
                    not isinstance(page, dict)
                    or set(page) != {"ids"}
                    or not isinstance(page["ids"], list)
                    or not all(isinstance(item, str) for item in page["ids"])
                    or not isinstance(done, bool)
                ):
                    self._malformed_response(operation, "invalid stream page contract")
                if page["ids"]:
                    pages.append(page["ids"])
                if done:
                    return pages

    @staticmethod
    def _is_lock_error(error: _WorkerOperationError) -> bool:
        detail = error.detail.lower()
        return "can't lock" in detail or "cannot lock" in detail

    @staticmethod
    def _payload_paths(payload: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        path = payload.get("path")
        if isinstance(path, str):
            paths.append(path)
        raw_paths = payload.get("paths")
        if isinstance(raw_paths, list):
            paths.extend(item for item in raw_paths if isinstance(item, str))
        batches = payload.get("batches")
        if isinstance(batches, list):
            paths.extend(
                batch["path"]
                for batch in batches
                if isinstance(batch, dict) and isinstance(batch.get("path"), str)
            )
        return list(dict.fromkeys(paths))

    def _raise_reclaim_timeout(
        self,
        operation: str,
        detail: str,
        cause: BaseException,
    ) -> None:
        raise RetrievalProviderError(
            self.provider_id,
            f"Zvec collection reclaim timed out after {self._reclaim_timeout_s:.2f}s "
            f"during {operation}: {detail}",
        ) from cause

    def _call_with_reclaim(self, operation: str, **payload: Any) -> dict[str, Any]:
        with self._ipc_lock:
            if self._child is None:
                self._start_child()
        overall_deadline = time.monotonic() + self._reclaim_timeout_s
        self._last_reclaim_retries = 0
        while True:
            request_deadline = min(
                overall_deadline,
                time.monotonic() + self._request_timeout_s,
            )
            try:
                result = self._request(operation, deadline=request_deadline, **payload)
            except RetrievalProviderError as exc:
                remaining = overall_deadline - time.monotonic()
                if "timed out" in str(exc).lower() and remaining <= 0.01:
                    self._last_reclaim_retries += 1
                    self._degraded_paths = self._payload_paths(payload)
                    self._last_error = str(exc)
                    self._raise_reclaim_timeout(operation, str(exc), exc)
                raise
            except _WorkerOperationError as exc:
                detail = f"{exc.error_type}: {exc.detail}"
                self._last_error = detail
                if not self._is_lock_error(exc):
                    raise RetrievalProviderError(
                        self.provider_id, f"{operation} failed: {detail}"
                    ) from exc
                self._last_reclaim_retries += 1
                self._degraded_paths = self._payload_paths(payload)
                remaining = overall_deadline - time.monotonic()
                if remaining <= 0:
                    self._raise_reclaim_timeout(operation, detail, exc)
                time.sleep(min(_RECLAIM_BACKOFF_S, remaining))
                continue
            self._last_error = ""
            self._degraded_paths = []
            return result

    def _call_pages_with_reclaim(self, operation: str, **payload: Any) -> list[list[str]]:
        with self._ipc_lock:
            if self._child is None:
                self._start_child()
        overall_deadline = time.monotonic() + self._reclaim_timeout_s
        self._last_reclaim_retries = 0
        while True:
            request_deadline = min(
                overall_deadline,
                time.monotonic() + self._request_timeout_s,
            )
            try:
                pages = self._request_pages(
                    operation,
                    deadline=request_deadline,
                    **payload,
                )
            except RetrievalProviderError as exc:
                remaining = overall_deadline - time.monotonic()
                if "timed out" in str(exc).lower() and remaining <= 0.01:
                    self._last_reclaim_retries += 1
                    self._degraded_paths = self._payload_paths(payload)
                    self._last_error = str(exc)
                    self._raise_reclaim_timeout(operation, str(exc), exc)
                raise
            except _WorkerOperationError as exc:
                detail = f"{exc.error_type}: {exc.detail}"
                self._last_error = detail
                if not self._is_lock_error(exc):
                    raise RetrievalProviderError(
                        self.provider_id, f"{operation} failed: {detail}"
                    ) from exc
                self._last_reclaim_retries += 1
                self._degraded_paths = self._payload_paths(payload)
                remaining = overall_deadline - time.monotonic()
                if remaining <= 0:
                    self._raise_reclaim_timeout(operation, detail, exc)
                time.sleep(min(_RECLAIM_BACKOFF_S, remaining))
                continue
            self._last_error = ""
            self._degraded_paths = []
            return pages

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
            response = self._call_with_reclaim("index", batches=batches)
            written = response.get("written")
            if written != len(items):
                self._malformed_response(
                    "index", f"worker reported written={written!r} for {len(items)} items"
                )

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

        source_fact_id = _source_filter_value(filters, self.provider_id)
        t0 = time.monotonic()
        response = self._call_with_reclaim(
            "query",
            path=str(path),
            vector=vector,
            k=k,
            source_fact_id=source_fact_id,
        )
        raw_hits = response.get("hits")
        if not isinstance(raw_hits, list):
            self._malformed_response("query", "result.hits must be a list")
        hits: list[Hit] = []
        for item in raw_hits:
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "score", "payload"}
                or not isinstance(item["id"], str)
                or not isinstance(item["payload"], dict)
            ):
                self._malformed_response("query", "invalid hit object")
            try:
                score = float(item["score"])
            except (TypeError, ValueError) as exc:
                self._malformed_response("query", f"invalid hit score: {exc}")
            hits.append(Hit(id=item["id"], score=score, payload=item["payload"]))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return RankedResults(
            hits=hits,
            provider_id=self.provider_id,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        targets = self._family_collections(store_id)
        if self._write_fence is not None:
            for collection, _, _ in targets:
                self._write_fence.assert_writable(collection)
        if not targets or not item_ids:
            return
        batches = [
            {
                "path": str(path),
                "point_ids": [str(_to_point_id(item_id)) for item_id in item_ids],
            }
            for _, path, _ in targets
        ]
        self._call_with_reclaim(
            "preflight_paths", paths=[batch["path"] for batch in batches]
        )
        self._call_with_reclaim("delete", batches=batches)

    def scroll_payload(
        self,
        store_id: str,
        payload_filter: dict,
        batch_size: int = 128,
    ):
        self._enforce(store_id, next(iter(self.capability_classes)))
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise RetrievalProviderError(self.provider_id, "batch_size must be a positive integer")
        source_fact_id = _source_filter_value(payload_filter, self.provider_id)
        targets = self._family_collections(store_id)
        if not targets:
            self._last_stream_page_sizes = []
            return iter(())
        pages = self._call_pages_with_reclaim(
            "scroll",
            source_fact_id=source_fact_id,
            batch_size=batch_size,
            paths=[str(path) for _, path, _ in targets],
        )
        self._last_stream_page_sizes = [len(page) for page in pages]
        return iter(point_id for page in pages for point_id in page)

    def health(self) -> HealthStatus:
        try:
            if self._degraded_paths:
                self._call_with_reclaim("preflight_paths", paths=list(self._degraded_paths))
            else:
                self._request("ping", deadline=time.monotonic() + self._request_timeout_s)
                self._last_error = ""
            return HealthStatus(ok=True)
        except (RetrievalProviderError, _WorkerOperationError) as exc:
            self._last_error = str(exc)
            return HealthStatus(ok=False, detail=self._last_error)

    def close(self) -> None:
        with self._ipc_lock:
            child = self._child
            if child is None:
                return
            if child.poll() is None:
                try:
                    self._request(
                        "shutdown",
                        deadline=time.monotonic() + self._request_timeout_s,
                    )
                except (RetrievalProviderError, _WorkerOperationError):
                    self._discard_child(kill=True)
                    return
            self._discard_child(kill=False)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _validate_collection_prefix(collection_prefix: str) -> None:
    if (
        not isinstance(collection_prefix, str)
        or not collection_prefix
        or collection_prefix.strip(".") == ""
        or _PREFIX_RE.fullmatch(collection_prefix) is None
    ):
        raise ValueError(
            "collection_prefix must be a single safe name component containing only "
            "letters, digits, dot, underscore, or hyphen"
        )


def _source_filter_value(
    filters: FilterExpr | None,
    provider_id: str,
) -> str | None:
    if not filters:
        return None
    if set(filters) != {_SOURCE_FIELD}:
        raise RetrievalProviderError(
            provider_id,
            "unsupported Zvec equality filters; supported keys: source_fact_id",
        )
    value = filters[_SOURCE_FIELD]
    if not isinstance(value, str):
        raise RetrievalProviderError(provider_id, "source_fact_id filter must be a string")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_main() -> int:
    import zvec

    collections: dict[str, Any] = {}
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise ValueError("request must be an object with a string op")
            operation = request["op"]
            if operation == "shutdown":
                collections.clear()
                gc.collect()
                _write_response({"ok": True, "result": {}})
                return 0
            if operation == "scroll":
                _worker_scroll(zvec, collections, request)
                continue
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


def _worker_dispatch(
    zvec,
    collections: dict[str, Any],
    operation: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    if operation == "ping":
        return {}
    if operation == "preflight_paths":
        _worker_preflight_paths(zvec, collections, request["paths"])
        return {}
    if operation == "index":
        return _worker_index(zvec, collections, request["batches"])
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
            output_fields=[
                _SOURCE_FIELD,
                _SOURCE_STATE_FIELD,
                _CHUNK_FIELD,
                _PAYLOAD_FIELD,
            ],
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
            existing_ids = [
                str(item.id) if hasattr(item, "id") else str(item)
                for item in existing
            ]
            if existing_ids:
                _assert_success(collection.delete(existing_ids))
                collection.flush()
        return {}
    raise ValueError(f"unknown operation {operation!r}")


def _worker_preflight_paths(
    zvec,
    collections: dict[str, Any],
    paths: list[str],
) -> None:
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("preflight paths must be a list of strings")
    opened_here: list[str] = []
    try:
        for path in dict.fromkeys(paths):
            if path in collections or not Path(path).is_dir():
                continue
            _worker_collection(
                zvec,
                collections,
                path=path,
                collection_name="",
                dim=0,
                create=False,
            )
            opened_here.append(path)
    except Exception:
        for path in reversed(opened_here):
            collection = collections.pop(path, None)
            if collection is not None:
                del collection
        gc.collect()
        raise


def _worker_index(
    zvec,
    collections: dict[str, Any],
    batches: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(batches, list):
        raise ValueError("index batches must be a list")

    prepared: list[tuple[dict[str, Any], list[Any]]] = []
    total = 0
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError("each index batch must be an object")
        dim = int(batch["dim"])
        items = batch["items"]
        if dim <= 0 or not isinstance(items, list):
            raise ValueError("index batch dimension and items are invalid")
        docs = [_worker_build_doc(zvec, item, dim) for item in items]
        prepared.append((batch, docs))
        total += len(docs)

    acquired: list[tuple[Any, list[Any]]] = []
    opened_here: list[str] = []
    try:
        for batch, docs in prepared:
            path = batch["path"]
            was_open = path in collections
            collection = _worker_collection(
                zvec,
                collections,
                path=path,
                collection_name=batch["collection"],
                dim=int(batch["dim"]),
                create=True,
            )
            if not was_open:
                opened_here.append(path)
            acquired.append((collection, docs))
    except Exception:
        _worker_release_paths(collections, opened_here)
        raise

    confirmed = 0
    for collection, docs in acquired:
        for offset in range(0, len(docs), _MAX_UPSERT_BATCH):
            write_batch = docs[offset : offset + _MAX_UPSERT_BATCH]
            try:
                _assert_success(collection.upsert(write_batch))
                collection.flush()
            except Exception as exc:
                raise RuntimeError(
                    f"index partial write: confirmed {confirmed} of {total} docs written; "
                    f"outcome of failing {len(write_batch)}-doc batch is unknown: {exc}"
                ) from exc
            confirmed += len(write_batch)
    return {"written": confirmed}


def _worker_release_paths(
    collections: dict[str, Any],
    paths: list[str],
) -> None:
    for path in reversed(paths):
        collection = collections.pop(path, None)
        if collection is not None:
            del collection
    gc.collect()


def _worker_build_doc(zvec, item: dict[str, Any], dim: int):
    if not isinstance(item, dict):
        raise ValueError("each index item must be an object")
    vector = item["vector"]
    if (
        not isinstance(vector, list)
        or len(vector) != dim
        or any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in vector
        )
    ):
        raise ValueError(f"embedding must contain exactly {dim} numeric values")
    raw_payload = item["payload"]
    if not isinstance(raw_payload, dict):
        raise ValueError("payload must be an object")
    payload = dict(raw_payload)
    source_present = _SOURCE_FIELD in payload
    source_fact_id = payload.pop(_SOURCE_FIELD, None)
    payload.pop(_CHUNK_FIELD, None)
    if not source_present:
        source_state = _SOURCE_STATE_MISSING
        source_value = ""
    elif source_fact_id is None:
        source_state = _SOURCE_STATE_NONE
        source_value = ""
    elif isinstance(source_fact_id, str):
        source_state = _SOURCE_STATE_VALUE
        source_value = source_fact_id
    else:
        raise ValueError("source_fact_id must be a string or None when supplied")
    payload_json = json.dumps(payload, separators=(",", ":"))
    return zvec.Doc(
        id=str(_to_point_id(item["id"])),
        vectors={_VECTOR_FIELD: vector},
        fields={
            _SOURCE_FIELD: source_value,
            _SOURCE_STATE_FIELD: source_state,
            _CHUNK_FIELD: str(item["id"]),
            _PAYLOAD_FIELD: payload_json,
        },
    )

def _worker_scroll(
    zvec,
    collections: dict[str, Any],
    request: dict[str, Any],
) -> None:
    paths = request["paths"]
    batch_size = int(request["batch_size"])
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    _worker_preflight_paths(zvec, collections, paths)
    pending: list[str] = []
    for path in paths:
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
            topk=doc_count,
            filter=_zvec_filter(request.get("source_fact_id")),
            output_fields=[_CHUNK_FIELD],
        )
        for doc in docs:
            pending.append(str(doc.id))
            if len(pending) == batch_size:
                _write_response({"ok": True, "page": {"ids": pending}, "done": False})
                pending = []
    _write_response({"ok": True, "page": {"ids": pending}, "done": True})


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
                zvec.FieldSchema(_SOURCE_STATE_FIELD, zvec.DataType.STRING),
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
    escaped = source_fact_id.replace("'", "\\'")
    return (
        f"{_SOURCE_STATE_FIELD} = '{_SOURCE_STATE_VALUE}' AND "
        f"{_SOURCE_FIELD} = '{escaped}'"
    )


def _worker_doc_result(doc) -> dict[str, Any]:
    fields = dict(doc.fields or {})
    payload_raw = fields.get(_PAYLOAD_FIELD, "{}")
    payload = json.loads(payload_raw)
    if not isinstance(payload, dict):
        raise ValueError("payload_json must decode to an object")

    source_state = fields.get(_SOURCE_STATE_FIELD)
    source_value = fields.get(_SOURCE_FIELD)
    if source_state == _SOURCE_STATE_NONE:
        payload[_SOURCE_FIELD] = None
    elif source_state == _SOURCE_STATE_VALUE:
        if not isinstance(source_value, str):
            raise ValueError("canonical source_fact_id value must be a string")
        payload[_SOURCE_FIELD] = source_value
    elif source_state != _SOURCE_STATE_MISSING:
        raise ValueError(f"invalid source_fact_id state {source_state!r}")

    payload[_CHUNK_FIELD] = fields.get(_CHUNK_FIELD, "")
    return {"id": str(doc.id), "score": float(doc.score or 0.0), "payload": payload}

if __name__ == "__main__":
    if "--worker" not in sys.argv:
        raise SystemExit("zvec_provider is a library module")
    raise SystemExit(_worker_main())
