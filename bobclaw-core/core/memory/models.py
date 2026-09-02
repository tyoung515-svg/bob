from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    body: dict
    ts: str
    hash: str
    prev_hash: str | None


@dataclass(frozen=True)
class ConfidenceStub:
    alpha: float = 1.0
    beta: float = 1.0
    rank: Literal["deprecated", "normal", "preferred"] = "normal"
    decay_class: str = "stable_biographical"
    last_corroboration_event_id: str | None = None
    last_corroboration_ts: str | None = None


@dataclass(frozen=True)
class AttestationEnvelope:
    producer_id: str
    producer_hash: str
    producer_signature: str
    produced_at: str
    runtime_env_hash: str


@dataclass(frozen=True)
class Fact:
    fact_id: str
    generation_method: str
    body: dict
    source_event_id: str
    input_hash: str
    confidence: ConfidenceStub
    ts: str
    attestation: AttestationEnvelope | None = None


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    fact_ids: list[str]
    spec_version: str
    input_hash: str


@dataclass(frozen=True)
class Chunk:
    text: str
    heading_path: list[str]
    chunk_hash: str = ""
    source_fact_id: str | None = None

    def __post_init__(self) -> None:
        if not self.chunk_hash:
            h = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
            object.__setattr__(self, "chunk_hash", h)


@dataclass(frozen=True)
class ChunkRecord:
    id: str
    vector: list[float]
    payload: dict


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    payload: dict


@dataclass(frozen=True)
class RetrievedChunk:
    content: str
    score: float
    source_fact_id: str | None
    source_path: str | None
    heading_path: list[str]
    boosted_score: float | None = None


@dataclass
class IndexStats:
    chunks_changed: int = 0
    chunks_skipped: int = 0
    chunks_deleted: int = 0
    facts_processed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class IndexReceipt:
    provider_id: str
    store_id: str
    item_count: int
    ts: str


@dataclass(frozen=True)
class Query:
    text: str
    capability_class: str


FilterExpr = dict[str, Any]


@dataclass(frozen=True)
class RankedResults:
    hits: list[Hit]
    provider_id: str
    latency_ms: int


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    detail: str = ""


CapabilityClass = Literal[
    "text_dense",
    "text_sparse",
    "multimodal_dense",
    "visual_doc_late_interaction",
    "managed_remote",
    "rerank_cross",
]


@dataclass(frozen=True)
class SlotResolution:
    slot_name: str
    model: str
    backend: str
    endpoint: str
    embedding_dimension: int | None = None
