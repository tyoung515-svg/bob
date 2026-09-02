from __future__ import annotations

import asyncio
import logging
import os
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
    writer_enabled: bool = False
    t0_recall_enabled: bool = False
    sqlite_path: Path = _PROJECT_ROOT / ".memory" / "bobclaw_memory.db"
    qdrant_url: str = "http://localhost:6353"  # BoB's own Qdrant, NOT the shared LKS :6333
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
            writer_enabled=config.MEMORY_WRITER_ENABLED,
            t0_recall_enabled=config.MEMORY_T0_RECALL_ENABLED,
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
    writer: Any | None = None
    completion_ledger: Any | None = None
    pending_extraction_tasks: set[asyncio.Task] = field(default_factory=set)
    last_extraction_error: Exception | None = None
    # L0 persistence is best-effort for chat availability; retain the most
    # recent failure for health/diagnostic surfaces.
    last_l0_append_error: Exception | None = None
    # Set by the recall path when it fails open (embedder/Qdrant unavailable);
    # cleared on the next healthy recall. Observable, mirrors last_extraction_error.
    last_recall_error: Exception | None = None
    # W3 mirrors the L1 diagnostics while keeping the zero-LLM task independent.
    last_writer_error: Exception | None = None
    pending_writer_tasks: set[asyncio.Task] = field(default_factory=set)

    async def drain_extraction_tasks(self) -> None:
        tasks = list(self.pending_extraction_tasks)
        if not tasks:
            return
        self.pending_extraction_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain_writer_tasks(self) -> None:
        tasks = list(self.pending_writer_tasks)
        if not tasks:
            return
        self.pending_writer_tasks.clear()
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


def _maybe_build_write_fence(slot_resolver: SlotResolver, collection_prefix: str):
    """Build a default-OFF single-writer ``WriteFence`` (MS2-C4); ``None`` unless ``MEMORY_WRITE_FENCE_ENABLED``.

    Default (flag unset/false) returns ``None`` immediately so the legacy bootstrap + every existing test is
    byte-identical (no registry load, no behaviour change). When enabled, load the federation registry (default
    path / ``BOBCLAW_LEDGER_INSTANCES``), ensure the ``bobclaw-memory`` instance is registered for the SAME
    collection the provider writes — ``f"{collection_prefix}_{dim}"`` (matching ``_collection_name``) so a
    non-default ``collection_prefix`` can never cause a false-positive write denial — with an embed fingerprint
    derived from the live ``embed_text`` slot (no model-name literal here), and return a ``WriteFence`` owned by
    ``bobclaw`` so BoB's index/upsert/delete path writes ONLY its own collection.
    """
    import os

    if not (
        _consolidation_enabled()
        or os.environ.get("MEMORY_WRITE_FENCE_ENABLED", "false").strip().lower() in (
            "1", "true", "yes", "on",
        )
    ):
        return None

    from core.ledger.federation import FederationRegistry, default_registry_path
    from core.memory.fingerprint import fingerprint_from_slot
    from core.memory.write_fence import (
        WriteFence,
        register_bobclaw_memory,
        BOBCLAW_MEMORY_INSTANCE,
    )

    registry = FederationRegistry(default_registry_path()).load()
    fingerprint = fingerprint_from_slot(slot_resolver.get("embed_text"))
    # derive the collection from the SAME prefix the provider uses (QdrantRetrievalProvider
    # `_collection_name(dim)` == f"{collection_prefix}_{dim}") so the fence allows BoB's real write.
    collection = f"{collection_prefix}_{fingerprint.dim}"
    # ALWAYS (re-)register the CANONICAL bobclaw-memory record (live collection, writer=bobclaw, mode=rw,
    # fresh fingerprint). Reconciling unconditionally means a stale/corrupted entry — a prior dim, a changed
    # prefix, a wrong writer, or a hand-edit — can NEVER false-positive-deny the provider's real writes; a
    # genuine collection conflict (another instance owns this name) still surfaces loudly via register().
    register_bobclaw_memory(registry, fingerprint, collection=collection, overwrite=True)
    # D24 item 3: the registration must be VISIBLE to the federation ledger, not merely
    # enforced in-process. Persist the reconciled registry so the on-disk JSON (and any
    # DAG-ledger consumer of it) sees bobclaw-memory as an owned, writable instance.
    registry.save()
    return WriteFence(registry, owner="bobclaw")


def _maybe_build_lks_adapter(slot_resolver: SlotResolver, qdrant_client):
    """Build a default-OFF LKS-first read seam (MS2-C5); ``(None, None, False)`` unless ``MEMORY_LKS_FIRST``.

    Default (flag unset/false) returns ``(None, None, False)`` immediately so the legacy bootstrap + every
    existing test constructs ``MemoryRetriever`` byte-identically (no registry load, no adapter, no behaviour
    change). When enabled AND ``MEMORY_LKS_INSTANCE`` is set, load the federation registry (default path /
    ``BOBCLAW_LEDGER_INSTANCES``) and build a C3 ``LKSReadAdapter`` over the live LKS read client
    (``MEMORY_LKS_QDRANT_URL`` if set, else the provider's own ``qdrant_client`` — C5 does NOT repoint the
    write-side ``MEMORY_QDRANT_URL``), the C1 ``embed_text`` embedder, and the HARD-STAMP posture
    (``require_stamp=True`` + ``require_acl=True``). Returns ``(adapter, instance_name, True)``.

    P4/D7 closed the stamp gate (was ``require_stamp=False``): the soft path existed because corpus
    instances carried ``meta.acl`` but no ``meta.embed``. With the gate hard, a same-dim model swap — the
    one corruption the dim-suffix cannot catch — fail-closes instead of silently returning garbage.

    **Missing stamp vs mismatched stamp are different failures and are handled differently.** P4 stamped
    ``meta.embed`` on the ``wiki`` instance, but NOT on every registerable instance (3 of 5 records in the
    shipped ``ledger_instances.example.json`` are still ``acl``-without-``embed``), so the soft path's
    rationale is retired for ``wiki`` only — NOT for the registry at large. That matters, because
    ``FingerprintMissing`` does **not** degrade: ``retriever._search_lks_first`` re-raises it and
    ``graph._recall_node_wrapper`` doesn't catch it, so an unstamped instance would kill **every chat
    turn**. So this seam PRE-FLIGHTS the configured instance once, here:

    * **unknown instance / missing registry** (the registry is gitignored, and ``load()`` returns an EMPTY
      registry for a missing file rather than raising) ⇒ degrade to OFF, loudly, once — instead of a
      per-query ``FederationError`` that falls back quietly and looks like it works;
    * **known but unstamped** ⇒ degrade to OFF, loudly, once. A missing stamp is a PROVISIONING problem;
      recall must keep working via BoB (*"the cut-over may fall back, but it must never BREAK recall"*);
    * **stamped but MISMATCHED** ⇒ still fail-closed at query time. That is drift, it is D7's actual
      target, and it must be loud.

    ``live_slot`` is REQUIRED, not optional: ``LKSReadAdapter.search`` refuses to read a stamped instance
    it cannot verify, and that refusal (``ReadAdapterError``) is on the retriever's FALLBACK list — so
    omitting ``live_slot`` would silently route every read back to BoB's store with no error surfaced.

    Construction + pre-flight are shared with the research lane via ``build_lks_read_adapter`` (one
    hard-posture builder, two seams); only the MEMORY_LKS_* env gating lives here.
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

    adapter, ok = build_lks_read_adapter(
        slot_resolver, qdrant_client, (instance,), seam="MEMORY_LKS_FIRST (recall seam)"
    )
    if adapter is None or instance not in ok:
        return (None, None, False)
    return (adapter, instance, True)


def build_lks_read_adapter(
    slot_resolver: SlotResolver, qdrant_client, instances, *, seam: str
):
    """Shared hard-posture ``LKSReadAdapter`` construction + per-instance pre-flight.

    ONE builder for both federated read seams — the C5 recall seam (``_maybe_build_lks_adapter``)
    and the research lane (``core.research.wiring``) — so the read posture can never drift between
    them: live LKS read client (``MEMORY_LKS_QDRANT_URL`` if set, else the provider's own
    ``qdrant_client``), the C1 ``embed_text`` embedder, ``live_slot`` REQUIRED, ``reader_id="bobclaw"``,
    ``require_stamp=True`` + ``require_acl=True``.

    Returns ``(adapter, ok_instances)`` — ``ok_instances`` is the subset of *instances* that resolved
    in the federation registry AND carry a ``meta.embed`` stamp, in input order; ``(None, ())`` when
    nothing survives. Degrade rules (the strangler's safety posture — an opt-in read seam may fall
    back, but must never BREAK its host):

    * ANY construction failure (missing/malformed registry, embedder slot, client) ⇒ ``(None, ())``
      with a logged warning, never a raise;
    * an UNKNOWN or UNSTAMPED instance is dropped loudly here, once — ``FingerprintMissing`` does NOT
      degrade at query time (it propagates and would kill every consuming turn), so pre-flight is the
      only safe place to catch a provisioning problem;
    * a stamped-but-MISMATCHED instance is deliberately NOT filtered — that is drift (D7's actual
      target) and must stay loud, fail-closed, at query time.

    ``seam`` labels the log lines so a degrade is attributable to the seam that configured it.
    """
    import os

    from core.ledger.federation import FederationRegistry, default_registry_path
    from core.memory.fingerprint import read_meta_fingerprint
    from core.memory.lks_adapter import LKSReadAdapter

    wanted = tuple(instances)
    if not wanted:
        return (None, ())

    try:
        registry = FederationRegistry(default_registry_path()).load()
        lks_url = os.environ.get("MEMORY_LKS_QDRANT_URL", "").strip()
        client = QdrantClient(url=lks_url, timeout=10) if lks_url else qdrant_client
        embedder = SlotResolvedEmbedder(slot_resolver, "embed_text")
        adapter = LKSReadAdapter(
            registry,
            client=client,
            embedder=embedder,
            live_slot=slot_resolver.get("embed_text"),  # REQUIRED — without it a stamped instance
                                                        # fails to ReadAdapterError -> silent fallback
            reader_id="bobclaw",
            require_stamp=True,    # P4/D7: meta.embed is stamped; the C2 gate binds (fail-closed)
            require_acl=True,      # enforce the C4-backfilled read-only ACL
        )
    except Exception as exc:  # noqa: BLE001 — wiring must degrade, never crash the host subsystem
        log.warning(
            "%s: LKS read adapter could not be built for instances %r (%s: %s); "
            "federated LKS reads stay OFF for this seam",
            seam, wanted, type(exc).__name__, exc,
        )
        return (None, ())

    ok: list[str] = []
    for inst in wanted:
        # resolve() raises FederationError for an unknown instance — including every instance when the
        # gitignored registry file is simply absent, since load() yields an EMPTY registry rather than raising.
        try:
            stamped = read_meta_fingerprint(registry.resolve(inst).meta) is not None
        except Exception as exc:  # noqa: BLE001 — an unknown instance is dropped, never a crash
            log.warning(
                "%s: LKS instance %r is unknown/unresolvable (%s: %s); instance dropped",
                seam, inst, type(exc).__name__, exc,
            )
            continue
        if not stamped:
            # Loud ONCE here, rather than FingerprintMissing on every read — which propagates out of the
            # consuming turn. Drift (a MISMATCHED stamp) still fail-closes at query time.
            log.warning(
                "%s: LKS instance %r carries no meta.embed stamp; under the hard stamp gate that would "
                "raise FingerprintMissing on EVERY read (it propagates — it does NOT fall back), so the "
                "instance is dropped. Stamp the instance's meta.embed to enable it.",
                seam, inst,
            )
            continue
        ok.append(inst)

    if not ok:
        return (None, ())
    return (adapter, tuple(ok))


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
                or config.writer_enabled != _bootstrap_config_snapshot.writer_enabled
                or config.t0_recall_enabled != _bootstrap_config_snapshot.t0_recall_enabled
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
        # MEMORY_SLOTS_FILE overrides the in-tree default — the per-environment
        # overlay mechanism the ops setup assumes (it was silently ignored
        # here until 2026-07-24, leaving the tracked toml as the real config).
        slots_env = os.getenv("MEMORY_SLOTS_FILE", "").strip()
        slots_path = (
            Path(slots_env).expanduser()
            if slots_env
            else _PROJECT_ROOT / "config" / "memory_slots.toml"
        )
        log.info("Reading memory slots config: %s", slots_path)
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
        # MS2-C4: build the single-writer write fence (default-OFF; None ⇒ legacy bootstrap byte-identical).
        _collection_prefix = first_pconf.get("collection_prefix", "bobclaw_")
        write_fence = _maybe_build_write_fence(slot_resolver, _collection_prefix)
        if config.writer_enabled and write_fence is None:
            raise MemoryConfigError(
                "MEMORY_WRITER_ENABLED requires MEMORY_WRITE_FENCE_ENABLED=1; "
                "refusing to build an unfenced writer"
            )
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
            t0_recall_enabled=config.t0_recall_enabled,
        )

        indexer = MemoryIndexer(
            fact_store=fact_store,
            embedder=embedder,
            provider=provider,
            store_id=config.default_store_id,
            slot_resolver=slot_resolver,
        )

        completion_ledger = None
        writer = None
        if config.writer_enabled:
            from core.memory.writer.ledger import CompletionLedger
            from core.memory.writer.tasks import ProjectVerbatimTask

            completion_ledger = CompletionLedger(config.sqlite_path)
            _run_coro_blocking(completion_ledger.initialize())
            writer = ProjectVerbatimTask(
                completion_ledger,
                embedder,
                provider,
                config.default_store_id,
            )

        singletons = MemorySingletons(
            event_log=event_log,
            fact_store=fact_store,
            retriever=retriever,
            indexer=indexer,
            acl_registry=acl_registry,
            slot_resolver=slot_resolver,
            extractor=extractor,
            writer=writer,
            completion_ledger=completion_ledger,
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
