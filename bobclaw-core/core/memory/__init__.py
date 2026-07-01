from __future__ import annotations

from core.memory.interfaces import (
    Embedder,
    EventLog,
    FactStore,
    Indexer,
    Renderer,
    RetrievalProvider,
    Retriever,
    Splicer,
)
from core.memory.models import (
    AttestationEnvelope,
    CapabilityClass,
    Chunk,
    ChunkRecord,
    ConfidenceStub,
    Event,
    Fact,
    FilterExpr,
    HealthStatus,
    Hit,
    IndexReceipt,
    IndexStats,
    Query,
    RankedResults,
    RetrievedChunk,
    Section,
    SlotResolution,
)
from core.memory.exceptions import (
    ACLViolation,
    AttestationError,
    EmbedderUnavailable,
    HashAllowlistMissing,
    HashingError,
    HopBudgetExceeded,
    L0AppendFailed,
    L1ValidationFailed,
    MemoryConfigError,
    MemoryError,
    RenderFailed,
    RetrievalProviderError,
    SchemaEvolutionError,
    SlotDeferred,
    SlotMisconfigured,
    SpliceFailed,
    TruthMaintenanceError,
)
from core.memory.acl import ACLRegistry, StoreACL
from core.memory.attestation import create_attestation, verify_attestation
from core.memory.schema_evolution import (
    apply_upcaster_chain,
    get_upcaster_chain,
    register_upcaster,
    upgrade_body_to_latest,
)
from core.memory._rrf import rrf_fuse
from core.memory.config import MemoryConfig
from core.memory.decay import credibility_mean
from core.memory.providers import QdrantRetrievalProvider
from core.memory.renderer import JinjaRenderer
from core.memory.splicer import MechanicalSplicer
from core.memory.indexer import MemoryIndexer
from core.memory.parser import ParsedDocument, parse_markdown
from core.memory.query_log import QueryLog
from core.memory.retriever import MemoryRetriever
from core.memory.truth_maintenance import TruthMaintenancePipeline
from core.memory.watcher import WikiWatcher
from core.memory.slots import SlotResolver

__all__ = [
    # Protocols
    "EventLog",
    "FactStore",
    "Splicer",
    "Renderer",
    "Embedder",
    "RetrievalProvider",
    "Indexer",
    "Retriever",
    # Models
    "AttestationEnvelope",
    "Event",
    "Fact",
    "ConfidenceStub",
    "Section",
    "Chunk",
    "ChunkRecord",
    "Hit",
    "RetrievedChunk",
    "IndexStats",
    "IndexReceipt",
    "Query",
    "FilterExpr",
    "RankedResults",
    "HealthStatus",
    "CapabilityClass",
    "SlotResolution",
    # Exceptions
    "MemoryError",
    "MemoryConfigError",
    "HashingError",
    "HashAllowlistMissing",
    "L0AppendFailed",
    "L1ValidationFailed",
    "SpliceFailed",
    "RenderFailed",
    "EmbedderUnavailable",
    "RetrievalProviderError",
    "SlotDeferred",
    "SlotMisconfigured",
    "HopBudgetExceeded",
    "ACLViolation",
    "TruthMaintenanceError",
    "SchemaEvolutionError",
    "AttestationError",
    # Concrete implementations
    "SQLiteEventLog",
    "SQLiteFactStore",
    # Providers
    "QdrantRetrievalProvider",
    # ACL
    "ACLRegistry",
    "StoreACL",
    # Config
    "MemoryConfig",
    # Parser
    "ParsedDocument",
    "parse_markdown",
    # Slot resolver
    "SlotResolver",
    # RRF
    "rrf_fuse",
    # Splicer
    "MechanicalSplicer",
    # Renderer
    "JinjaRenderer",
    # Watcher
    "WikiWatcher",
    # Indexer
    "MemoryIndexer",
    # Retriever
    "MemoryRetriever",
    # Truth-maintenance
    "TruthMaintenancePipeline",
    "credibility_mean",
    # Query log
    "QueryLog",
    # Schema evolution
    "register_upcaster",
    "get_upcaster_chain",
    "apply_upcaster_chain",
    "upgrade_body_to_latest",
    # Attestation
    "create_attestation",
    "verify_attestation",
]
