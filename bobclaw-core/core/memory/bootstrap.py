from __future__ import annotations

import asyncio
import logging
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qdrant_client import QdrantClient

from core.memory._db import init_schema
from core.memory.acl import ACLRegistry, StoreACL
from core.memory.embedder import SlotResolvedEmbedder
from core.memory.event_log import SQLiteEventLog
from core.memory.exceptions import MemoryConfigError
from core.memory.fact_store import SQLiteFactStore
from core.memory.indexer import MemoryIndexer
from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
from core.memory.query_log import QueryLog
from core.memory.retriever import MemoryRetriever
from core.memory.slots import SlotResolver

if TYPE_CHECKING:
    from core.config import BoBClawConfig
    from core.memory.extractor import FactExtractor

log = logging.getLogger(__name__)


def _run_coro_blocking(coro):
    """Run *coro* to completion, whether or not an event loop is already running.

    ``bootstrap_memory`` is synchronous but is called from two contexts:
    pytest (no running loop → plain ``asyncio.run``) and aiohttp's async
    ``_on_startup`` hook (a loop IS running → ``asyncio.run`` would raise
    "cannot be called from a running event loop"). In the latter case we run
    the coroutine on a short-lived worker thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


_BOOTSTRAP_LOCK = threading.Lock()
_bootstrap_singleton: MemorySingletons | None = None
_bootstrap_config_snapshot: MemoryBootstrapConfig | None = None

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class MemoryBootstrapConfig:
    enabled: bool = False
    sqlite_path: Path = _PROJECT_ROOT / ".memory" / "bobclaw_memory.db"
    qdrant_url: str = "http://localhost:6333"
    stores_config_path: Path = (
        _PROJECT_ROOT / "config" / "memory_stores.toml"
    )
    default_store_id: str = "bobclaw_default"

    @classmethod
    def from_env(cls, config: BoBClawConfig) -> MemoryBootstrapConfig:
        def _resolve(p: str) -> Path:
            path = Path(p)
            if not path.is_absolute():
                path = _PROJECT_ROOT / path
            return path

        return cls(
            enabled=config.MEMORY_ENABLED,
            sqlite_path=_resolve(config.MEMORY_SQLITE_PATH),
            qdrant_url=config.MEMORY_QDRANT_URL,
            stores_config_path=_resolve(config.MEMORY_STORES_CONFIG_PATH),
            default_store_id=config.MEMORY_DEFAULT_STORE_ID,
        )


@dataclass
class MemorySingletons:
    event_log: SQLiteEventLog
    fact_store: SQLiteFactStore
    retriever: MemoryRetriever
    indexer: MemoryIndexer
    acl_registry: ACLRegistry
    slot_resolver: SlotResolver
    extractor: "FactExtractor"
    pending_extraction_tasks: set[asyncio.Task] = field(default_factory=set)
    last_extraction_error: Exception | None = None
    # Set by the recall path when it fails open (embedder/Qdrant unavailable);
    # cleared on the next healthy recall. Observable, mirrors last_extraction_error.
    last_recall_error: Exception | None = None
    # The held write fence is retained for health visibility and lifecycle cleanup.
    write_fence: Any = None

    @property
    def write_fence_degraded(self) -> bool:
        return bool(self.write_fence is not None and self.write_fence.degraded)

    async def drain_extraction_tasks(self) -> None:
        tasks = list(self.pending_extraction_tasks)
        if not tasks:
            return
        self.pending_extraction_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)


def _parse_stores_toml(path: Path) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        "stores": raw.get("stores", {}),
        "providers": raw.get("providers", {}),
    }


def _build_store_acls(parsed: dict[str, Any]) -> dict[str, StoreACL]:
    stores_raw = parsed.get("stores", {})
    providers_raw = parsed.get("providers", {})
    store_acls: dict[str, StoreACL] = {}
    for store_id, store_conf in stores_raw.items():
        allowed_provider_ids = frozenset(
            store_conf.get("acl_allowed_providers", [])
        )
        allowed_locality: set[str] = set()
        allowed_capability_classes: set[str] = set()
        for pid in allowed_provider_ids:
            pconf = providers_raw.get(pid, {})
            allowed_locality.add(pconf.get("locality", "local"))
            for cc in pconf.get("capability_classes", []):
                allowed_capability_classes.add(cc)
        store_acls[store_id] = StoreACL(
            store_id=store_id,
            allowed_locality=frozenset(allowed_locality),
            allowed_provider_ids=allowed_provider_ids,
            allowed_capability_classes=frozenset(allowed_capability_classes),
        )
    return store_acls


def _register_acls(
    acl_registry: ACLRegistry, store_acls: dict[str, StoreACL]
) -> None:
    object.__setattr__(acl_registry, "_stores", store_acls)


def _consolidation_enabled() -> bool:
    # Parse strictly == "true" to MATCH `config.MEMORY_SINGLE_QDRANT` (and every other MEMORY_* flag in
    # config.py: MEMORY_ENABLED / MEMORY_L1_EXTRACTION_ENABLED / MEMORY_LKS_FIRST) — the config attribute and
    # this seam MUST agree on the flag's effective state (audit r1; same lesson as the C5 MEMORY_LKS_FIRST seam).
    # A broader truthy set ("1"/"yes"/"on") here would read ON in bootstrap but OFF in the config attribute.
    import os
    return os.environ.get("MEMORY_SINGLE_QDRANT", "false").strip().lower() == "true"


def _maybe_build_write_fence(
    slot_resolver: SlotResolver,
    collection_prefix: str,
    qdrant_url: str = "http://localhost:6333",
):
    """Build the mandatory family-scoped ``WriteFence`` for a memory bootstrap.

    ``MEMORY_ENABLED=true`` reaches this seam unconditionally. The fence flag is
    default-on-with-memory; setting it to any explicit non-``true`` value is a
    configuration conflict, never an unfenced writer opt-out.
    """
    import os

    fence_flag = os.environ.get("MEMORY_WRITE_FENCE_ENABLED", "").strip().lower()
    if fence_flag and fence_flag != "true":
        raise MemoryConfigError(
            "MEMORY_ENABLED=true requires MEMORY_WRITE_FENCE_ENABLED=true; "
            "MEMORY_WRITE_FENCE_ENABLED=false cannot start an unfenced writer"
        )

    from core.ledger.federation import FederationRegistry, default_registry_path
    from core.memory.fingerprint import fingerprint_from_slot
    from core.memory.write_fence import (
        WriteFence,
        assert_registry_family_available,
        register_bobclaw_memory,
    )

    registry = FederationRegistry(default_registry_path()).load()
    # Validate the loaded namespace before overwrite=True can mutate a spoofed
    # reserved-name record or hide an external family owner.
    assert_registry_family_available(registry, collection_prefix)
    fingerprint = fingerprint_from_slot(slot_resolver.get("embed_text"))
    collection = f"{collection_prefix}_{fingerprint.dim}"
    register_bobclaw_memory(registry, fingerprint, collection=collection, overwrite=True)
    return WriteFence(
        registry,
        qdrant_url=qdrant_url,
        collection_prefix=collection_prefix,
        owner="bobclaw",
    )

def _maybe_build_lks_adapter(slot_resolver: SlotResolver, qdrant_client):
    """Build a default-OFF LKS-first read seam (MS2-C5); ``(None, None, False)`` unless ``MEMORY_LKS_FIRST``.

    Default (flag unset/false) returns ``(None, None, False)`` immediately so the legacy bootstrap + every
    existing test constructs ``MemoryRetriever`` byte-identically (no registry load, no adapter, no behaviour
    change). When enabled AND ``MEMORY_LKS_INSTANCE`` is set, load the federation registry (default path /
    ``BOBCLAW_LEDGER_INSTANCES``) and build a C3 ``LKSReadAdapter`` over the live LKS read client
    (``MEMORY_LKS_QDRANT_URL`` if set, else the provider's own ``qdrant_client`` — C5 does NOT repoint the
    write-side ``MEMORY_QDRANT_URL``), the C1 ``embed_text`` embedder, and the SOFT-STAMP posture
    (``require_stamp=False`` — corpus instances carry ``meta.acl`` (C4) but not ``meta.embed``, so an absent
    fingerprint is allowed while a present-but-mismatched one still fail-closes — paired with
    ``require_acl=True`` so the C4-backfilled ACL is enforced). Returns ``(adapter, instance_name, True)``.
    """
    import os

    # Parse strictly == "true" to match `config.MEMORY_LKS_FIRST` (and every other MEMORY_* flag in
    # config.py: MEMORY_ENABLED / MEMORY_L1_EXTRACTION_ENABLED / MEMORY_WATCH_WIKI) — the config attribute
    # and this seam must agree on the flag's effective state (audit r2). Default-OFF.
    if os.environ.get("MEMORY_LKS_FIRST", "false").strip().lower() != "true":
        return (None, None, False)

    instance = os.environ.get("MEMORY_LKS_INSTANCE", "").strip()
    if not instance:
        # Flag on but unconfigured ⇒ inert (fail-safe: never silently read a wrong/unintended instance).
        log.warning("MEMORY_LKS_FIRST is on but MEMORY_LKS_INSTANCE is unset; LKS-first stays OFF")
        return (None, None, False)

    from core.ledger.federation import FederationRegistry, default_registry_path
    from core.memory.lks_adapter import LKSReadAdapter

    # Graceful degrade (audit r2): a missing/malformed registry — or any adapter-construction failure — when
    # the opt-in flag is ON must NOT sink the whole memory bootstrap. Degrade to (None, None, False) with a
    # logged warning so recall keeps working via BoB's own store (the strangler's safety posture: the
    # cut-over may fall back, but it must never BREAK recall). A clean miss / availability error at read time
    # is handled separately by _search_lks_first; this guards the bootstrap-time wiring only.
    try:
        registry = FederationRegistry(default_registry_path()).load()
        lks_url = os.environ.get("MEMORY_LKS_QDRANT_URL", "").strip()
        client = QdrantClient(url=lks_url, timeout=10) if lks_url else qdrant_client
        embedder = SlotResolvedEmbedder(slot_resolver, "embed_text")
        adapter = LKSReadAdapter(
            registry,
            client=client,
            embedder=embedder,
            reader_id="bobclaw",
            require_stamp=False,   # soft path: corpus instances have meta.acl (C4) but no meta.embed
            require_acl=True,      # enforce the C4-backfilled read-only ACL
        )
    except Exception as exc:  # noqa: BLE001 — bootstrap-time wiring must degrade, never crash the subsystem
        log.warning(
            "MEMORY_LKS_FIRST is on but the LKS-first adapter could not be built (%s: %s); "
            "LKS-first stays OFF (recall falls back to BoB's own store)",
            type(exc).__name__, exc,
        )
        return (None, None, False)
    return (adapter, instance, True)


def _assert_single_qdrant_endpoint(qdrant_url: str) -> None:
    """MS2-C6: when consolidation is ON, enforce exactly ONE Qdrant endpoint (kills the two-Qdrant footgun)."""
    if not _consolidation_enabled():
        return
    import os
    lks_url = os.environ.get("MEMORY_LKS_QDRANT_URL", "").strip()
    if lks_url and lks_url != (qdrant_url or "").strip():
        raise MemoryConfigError(
            f"MEMORY_SINGLE_QDRANT is on but MEMORY_LKS_QDRANT_URL ({lks_url!r}) != MEMORY_QDRANT_URL "
            f"({qdrant_url!r}); the converged path must use exactly ONE Qdrant endpoint (registry-resolved "
            f"ownership). Leave MEMORY_LKS_QDRANT_URL empty to reuse the single provider client."
        )


def bootstrap_memory(config: MemoryBootstrapConfig) -> MemorySingletons:
    global _bootstrap_singleton, _bootstrap_config_snapshot

    with _BOOTSTRAP_LOCK:
        if _bootstrap_singleton is not None:
            if _bootstrap_config_snapshot is not None and (
                config.sqlite_path != _bootstrap_config_snapshot.sqlite_path
                or config.qdrant_url != _bootstrap_config_snapshot.qdrant_url
                or config.default_store_id
                != _bootstrap_config_snapshot.default_store_id
            ):
                raise MemoryConfigError(
                    "bootstrap already called with different config"
                )
            return _bootstrap_singleton

        log.info("Bootstrapping memory module")
        _assert_single_qdrant_endpoint(config.qdrant_url)

        log.info("Ensuring SQLite directory exists: %s", config.sqlite_path.parent)
        config.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        log.info("Initializing SQLite schema at %s", config.sqlite_path)
        try:
            _run_coro_blocking(init_schema(config.sqlite_path))
        except Exception as exc:
            raise MemoryConfigError(
                f"SQLite schema init failed at {config.sqlite_path}: {exc}"
            ) from exc

        log.info("Connecting to Qdrant at %s", config.qdrant_url)
        try:
            qdrant_client = QdrantClient(url=config.qdrant_url, timeout=10)
            qdrant_client.get_collections()
        except Exception as exc:
            raise MemoryConfigError(
                f"Qdrant unreachable at {config.qdrant_url} after 10s"
            ) from exc

        log.info("Reading stores config: %s", config.stores_config_path)
        slots_path = _PROJECT_ROOT / "config" / "memory_slots.toml"
        slot_resolver = SlotResolver(slots_path)

        parsed = _parse_stores_toml(config.stores_config_path)
        store_acls = _build_store_acls(parsed)

        acl_registry = ACLRegistry(config.stores_config_path)
        _register_acls(acl_registry, store_acls)

        providers_raw = parsed.get("providers", {})
        if not providers_raw:
            raise MemoryConfigError(
                f"No providers defined in {config.stores_config_path}"
            )

        first_pid, first_pconf = next(iter(providers_raw.items()))
        # MS2-C4/R3: memory-on bootstrap MUST arm a family fence or refuse to start.
        _collection_prefix = first_pconf.get("collection_prefix", "bobclaw_")
        try:
            write_fence = _maybe_build_write_fence(
                slot_resolver, _collection_prefix, config.qdrant_url
            )
        except MemoryConfigError:
            raise
        except Exception as exc:
            raise MemoryConfigError(
                f"memory write fence could not be armed: {type(exc).__name__}: {exc}"
            ) from exc
        if write_fence is None:
            raise MemoryConfigError("memory write fence could not be armed")
        provider = QdrantRetrievalProvider(
            provider_id=first_pid,
            locality=first_pconf.get("locality", "local"),
            collection_prefix=_collection_prefix,
            acl_registry=acl_registry,
            client=qdrant_client,
            write_fence=write_fence,
        )

        log.info("Building MemoryRetriever and MemoryIndexer")
        event_log = SQLiteEventLog(config.sqlite_path)
        fact_store = SQLiteFactStore(config.sqlite_path)

        from core.memory.extractor import FactExtractor

        extractor = FactExtractor(slot_resolver, fact_store)

        embedder = SlotResolvedEmbedder(slot_resolver, "embed_text")

        query_log_path = config.sqlite_path.parent / "query_log.jsonl"
        query_log = QueryLog(query_log_path)

        # MS2-C5: build the LKS-first read seam (default-OFF; (None, None, False) ⇒ retriever construction
        # byte-identical to today).
        lks_adapter, lks_instance, lks_first = _maybe_build_lks_adapter(slot_resolver, qdrant_client)
        retriever = MemoryRetriever(
            embedder=embedder,
            provider=provider,
            fact_store=fact_store,
            store_id=config.default_store_id,
            slot_resolver=slot_resolver,
            query_log=query_log,
            lks_adapter=lks_adapter,
            lks_instance=lks_instance,
            lks_first=lks_first,
        )

        indexer = MemoryIndexer(
            fact_store=fact_store,
            embedder=embedder,
            provider=provider,
            store_id=config.default_store_id,
            slot_resolver=slot_resolver,
        )

        singletons = MemorySingletons(
            event_log=event_log,
            fact_store=fact_store,
            retriever=retriever,
            indexer=indexer,
            acl_registry=acl_registry,
            slot_resolver=slot_resolver,
            extractor=extractor,
            write_fence=write_fence,
        )

        _bootstrap_singleton = singletons
        _bootstrap_config_snapshot = config

        log.info("Memory bootstrap complete")
        return singletons


def get_memory() -> MemorySingletons:
    if _bootstrap_singleton is None:
        raise MemoryConfigError(
            "memory not bootstrapped — call bootstrap_memory() first"
        )
    return _bootstrap_singleton
