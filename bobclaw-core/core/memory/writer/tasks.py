"""W3 writer task contracts and the zero-LLM T0 projection."""
from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from typing import Any

from core.memory.models import ChunkRecord, Event
from core.memory.writer.ledger import CompletionLedger


CHUNK_CHARS = 512
OVERLAP_CHARS = 100
T0_TASK_NAME = "project_verbatim"
T0_TASK_VERSION = "project_verbatim:v1"
T0_PROMPT_VERSION = "none"


def chunk_fixed(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Frozen W2 A2 chunker (W2's source function is named ``chunk_naive``).

    The body, header, 512-character budget, and 100-character overlap are an
    exact behavioural port.  Do not make sentence-aware or token-aware edits.
    """
    chunks = []
    for rec in records:
        header = f"[log {rec['event_id'][:8]} {rec['ts'][:10]}] "
        cap = CHUNK_CHARS - len(header)
        body = rec["assistant_response"].strip()
        if not body:
            continue
        step = max(1, cap - OVERLAP_CHARS)
        n = 0
        for start in range(0, len(body), step):
            piece = body[start:start + cap]
            if not piece.strip():
                break
            chunks.append({
                "chunk_id": f"{rec['event_id'][:8]}:{n}",
                "event_id": rec["event_id"],
                "ts": rec["ts"],
                "text": header + piece,
                "body_chars": len(piece),
            })
            n += 1
            if start + cap >= len(body):
                break
    return chunks


def normalized_content_hash(event: Event) -> str:
    """Hash only normalized content that T0 actually projects.

    Volatile event metadata and user/turn ids are deliberately excluded.  NFKC,
    newline normalization, whitespace folding, and case-folding collapse exact
    duplicate artifacts without changing the verbatim bytes stored in chunks.
    """
    response = event.body.get("assistant_response", "") if event.body else ""
    if not isinstance(response, str):
        response = str(response or "")
    normalized = unicodedata.normalize("NFKC", response.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = " ".join(normalized.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectResult:
    event_id: str
    status: str
    chunk_count: int = 0
    duplicate_of_event_id: str | None = None


class ProjectVerbatimTask:
    """T0: deterministic L0 event -> embedded verbatim chunk projection."""

    task_name = T0_TASK_NAME

    def __init__(
        self,
        ledger: CompletionLedger,
        embedder,
        provider,
        store_id: str,
        *,
        task_version: str = T0_TASK_VERSION,
        prompt_version: str = T0_PROMPT_VERSION,
        enabled: bool = True,
    ) -> None:
        self.ledger = ledger
        self.embedder = embedder
        self.provider = provider
        self.store_id = store_id
        self.task_version = task_version
        self.prompt_version = prompt_version
        self.enabled = enabled

    async def project(self, event: Event) -> ProjectResult:
        if not self.enabled or event.kind != "agent_turn":
            return ProjectResult(event.event_id, "disabled")

        source_hash = normalized_content_hash(event)
        claim = await self.ledger.claim(
            source_content_hash=source_hash,
            task_name=self.task_name,
            task_version=self.task_version,
            prompt_version=self.prompt_version,
            source_event_id=event.event_id,
        )
        if not claim.claimed:
            if claim.status == "busy":
                return ProjectResult(
                    event.event_id, "busy", duplicate_of_event_id=claim.source_event_id
                )
            status = "already_completed" if claim.source_event_id == event.event_id else "duplicate"
            return ProjectResult(
                event.event_id, status, duplicate_of_event_id=claim.source_event_id
            )

        try:
            if claim.claim_token is None:  # defensive: every winning claim owns a token
                raise RuntimeError("writer claim has no ownership token")
            chunks = chunk_fixed([{
                "event_id": event.event_id,
                "ts": event.ts,
                "assistant_response": (event.body or {}).get("assistant_response", "") or "",
            }])
            vectors = await self.embedder.embed([c["text"] for c in chunks]) if chunks else []
            if len(vectors) != len(chunks):
                raise ValueError(
                    f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
                )
            expected_dim = getattr(self.embedder, "embedding_dimension", None)
            if expected_dim is not None and any(len(v) != expected_dim for v in vectors):
                raise ValueError(f"embedding dimension does not match expected {expected_dim}")

            # A changed task/prompt contract replaces this event's old T0 points.
            # Delete-before-upsert is crash-safe because the ledger remains RUNNING;
            # stale reclaim deterministically rebuilds the same point ids.
            old_ids = list(self.provider.scroll_payload(
                self.store_id,
                {"projection_task": self.task_name, "source_event_id": event.event_id},
            ))
            if old_ids:
                self.provider.delete(self.store_id, old_ids)

            items: list[ChunkRecord] = []
            point_ids: list[str] = []
            for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                point_id = f"t0:{event.event_id}:{index}"
                point_ids.append(point_id)
                items.append(ChunkRecord(
                    id=point_id,
                    vector=vector,
                    payload={
                        "text": chunk["text"],
                        "projection_task": self.task_name,
                        "task_version": self.task_version,
                        "prompt_version": self.prompt_version,
                        "source_event_id": event.event_id,
                        "source_content_hash": source_hash,
                        "source_path": f"event://{event.event_id}",
                        "heading_path": [],
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "body_chars": chunk["body_chars"],
                        "source_ts": event.ts,
                        "w2_chunk_id": chunk["chunk_id"],
                    },
                ))
            if items:
                self.provider.index(self.store_id, items)
            await self.ledger.complete(
                source_content_hash=source_hash,
                task_version=self.task_version,
                prompt_version=self.prompt_version,
                claim_token=claim.claim_token,
                chunk_ids=point_ids,
            )
            return ProjectResult(event.event_id, "completed", len(point_ids))
        except Exception as exc:
            await self.ledger.fail(
                source_content_hash=source_hash,
                task_version=self.task_version,
                prompt_version=self.prompt_version,
                claim_token=claim.claim_token or "",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() == "true"


@dataclass(frozen=True)
class DeriveFactsTask:
    task_name: str = "derive_facts"
    enabled: bool = _flag("MEMORY_WRITER_T1_ENABLED", False)


@dataclass(frozen=True)
class TagTaskClassTask:
    task_name: str = "tag_task_class"
    enabled: bool = _flag("MEMORY_WRITER_T2_ENABLED", False)


@dataclass(frozen=True)
class ReconcileTask:
    task_name: str = "reconcile"
    enabled: bool = _flag("MEMORY_WRITER_T3_ENABLED", False)
