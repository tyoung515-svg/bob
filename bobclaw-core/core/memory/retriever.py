from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from core.memory._rrf import rrf_fuse
from core.memory.decay import credibility_mean
from core.memory.exceptions import HopBudgetExceeded, L1ValidationFailed
from core.memory.exceptions import EmbedderUnavailable, RetrievalProviderError, ACLViolation
from core.memory.fingerprint import FingerprintMismatch, FingerprintMissing
from core.memory.lks_adapter import ReadAdapterError
from core.ledger.federation import FederationError
from core.memory.models import Hit, RetrievedChunk

if TYPE_CHECKING:
    from core.memory.interfaces import Embedder, FactStore, RetrievalProvider
    from core.memory.query_log import QueryLog
    from core.memory.slots import SlotResolver

log = logging.getLogger(__name__)


class MemoryRetriever:
    def __init__(
        self,
        embedder: Embedder,
        provider: RetrievalProvider,
        fact_store: FactStore,
        store_id: str,
        slot_resolver: SlotResolver,
        query_log: QueryLog,
        *,
        lks_adapter=None,
        lks_instance=None,
        lks_first=False,
    ) -> None:
        self._embedder = embedder
        self._provider = provider
        self._fact_store = fact_store
        self._store_id = store_id
        self._slot_resolver = slot_resolver
        self._query_log = query_log
        self._lks_adapter = lks_adapter
        self._lks_instance = lks_instance
        self._lks_first = bool(lks_first)

    async def search(
        self,
        query: str,
        top_k: int = 3,
        threshold: float = 0.35,
        filters: dict | None = None,
        hop_budget: int = 1,
    ) -> list[RetrievedChunk]:
        # MS2-C5 strangler cut-over (OD#4): when MEMORY_LKS_FIRST is ON and an LKS instance +
        # adapter are wired, read the LKS corpus collection FIRST; on a miss / availability error
        # fall back to BoB's own store (the dangling-vector fail-open lives in _search_bob_store).
        # Flag OFF (default) ⇒ lks_first is False ⇒ this is byte-identical to the BoB-store path.
        if self._lks_first and self._lks_adapter is not None and self._lks_instance:
            lks = await self._search_lks_first(query, top_k, threshold, filters)
            if lks is not None:
                return lks
            # else: a MISS (zero hits) or a fallback-class error — fall through to BoB.
        return await self._search_bob_store(query, top_k, threshold, filters, hop_budget)

    async def _search_lks_first(
        self,
        query: str,
        top_k: int,
        threshold: float,
        filters: dict | None,
    ) -> list[RetrievedChunk] | None:
        """LKS-first read via the C3 adapter; None ⇒ miss/availability-error (fall back to BoB).

        Correctness/security signals (FingerprintMismatch/Missing, ACLViolation) PROPAGATE — a
        misconfigured cut-over must be loud, never masked by always falling back. Availability
        signals (embedder/provider/adapter/federation) degrade to BoB (logged, observable).
        """
        adapter_filters = dict(filters) if filters else {}
        adapter_filters.pop("include_deprecated", None)

        try:
            hits = await self._lks_adapter.search(
                self._lks_instance,
                query=query,
                k=top_k,
                filters=adapter_filters or None,
            )
        except (FingerprintMismatch, FingerprintMissing, ACLViolation):
            raise
        except (EmbedderUnavailable, RetrievalProviderError, ReadAdapterError, FederationError) as exc:
            log.warning(
                "recall: LKS-first read failed for instance %s (%s: %s); falling back to BoB store",
                self._lks_instance,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return None

        kept = [h for h in hits if h.score >= threshold]

        result: list[RetrievedChunk] = []
        for hit in kept:
            payload = hit.payload or {}
            content = payload.get("chunk_text") or payload.get("text") or payload.get("content") or ""
            heading_path = payload.get("heading_path") or []
            result.append(
                RetrievedChunk(
                    content=content,
                    score=hit.score,
                    # source_fact_id forced None: an LKS *corpus* chunk is NOT a BoB L1 fact — its id
                    # namespace is unrelated to BoB's fact store. Forcing None prevents a stray corpus
                    # `source_fact_id` payload from leaking into recall_node's cross-namespace fact lookup
                    # (audit r5). For C5's scope (corpus collections) this is a no-op — corpus payloads
                    # carry no source_fact_id key.
                    source_fact_id=None,
                    source_path=payload.get("source_path"),
                    heading_path=heading_path,
                    boosted_score=hit.score,
                )
            )

        result.sort(key=lambda r: r.boosted_score or 0, reverse=True)
        result = result[:top_k]
        return result if result else None

    async def _search_bob_store(
        self,
        query: str,
        top_k: int = 3,
        threshold: float = 0.35,
        filters: dict | None = None,
        hop_budget: int = 1,
    ) -> list[RetrievedChunk]:
        if hop_budget >= 3:
            raise HopBudgetExceeded(hop_budget, 2)

        self._slot_resolver.get("embed_text")

        include_deprecated = False
        provider_filters: dict[str, Any] = dict(filters) if filters else {}
        if "include_deprecated" in provider_filters:
            include_deprecated = provider_filters.pop("include_deprecated")

        query_vec = (await self._embedder.embed_query([query]))[0]

        initial_results = self._provider.query_vector(
            self._store_id, query_vec, top_k, provider_filters or None,
        )

        all_rankings: list[list] = [list(initial_results.hits)]
        max_nodes = 1

        if hop_budget == 2:
            wikilinks: set[str] = set()
            for hit in initial_results.hits:
                for wl in hit.payload.get("wikilinks", []):
                    wikilinks.add(wl)

            for wl_target in wikilinks:
                wl_vec = (await self._embedder.embed_query([wl_target]))[0]
                wl_results = self._provider.query_vector(
                    self._store_id, wl_vec, top_k, provider_filters or None,
                )
                all_rankings.append(list(wl_results.hits))

            max_nodes = len(wikilinks) + 1

        # NOTE: Spec §5.5 — federation normalizes via rank, never via raw score.
        # Thresholds are per-provider semantic guards applied pre-fusion.
        # See docs/archive/AUDIT_WAVE2_2026-05-12.md Wave 3 seed "RRF Threshold Semantics".
        filtered_rankings = [
            [h for h in ranking if h.score >= threshold]
            for ranking in all_rankings
        ]
        fused = rrf_fuse(filtered_rankings, k=60)

        seen_ids: set[str] = set()
        result_chunks: list[RetrievedChunk] = []

        for hit in fused:
            if hit.id in seen_ids:
                continue
            seen_ids.add(hit.id)

            source_fact_id: str | None = hit.payload.get("source_fact_id")
            raw_score = hit.score

            boosted = raw_score
            if source_fact_id:
                # Fail open on a dangling vector: if the fact was forgotten
                # (DELETE /api/memory/facts) but a vector point lingers, skip the
                # hit instead of letting L1ValidationFailed abort the whole turn.
                try:
                    fact = await self._fact_store.get(source_fact_id)
                except L1ValidationFailed:
                    log.warning(
                        "recall: skipping hit %s — fact %s not in FactStore "
                        "(forgotten or dangling vector)",
                        hit.id, source_fact_id,
                    )
                    continue
                # NOTE: Decay is applied at READ time per Hard Rule 15.
                # Confidence in storage reflects evidence, not the passage of
                # time — the decay transform is computed here, never written back.
                cred_mean = credibility_mean(fact.confidence, datetime.now(timezone.utc))
                boosted = raw_score * max(0.05, cred_mean)

                if not include_deprecated and fact.confidence.rank == "deprecated":
                    continue

            result_chunks.append(
                RetrievedChunk(
                    content=hit.payload.get("text", ""),
                    score=raw_score,
                    source_fact_id=source_fact_id,
                    source_path=hit.payload.get("source_path"),
                    heading_path=hit.payload.get("heading_path", []),
                    boosted_score=boosted,
                )
            )

        result_chunks.sort(key=lambda r: r.boosted_score or 0, reverse=True)
        result_chunks = result_chunks[:top_k]

        self._query_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "k": top_k,
            "hop_budget": hop_budget,
            "max_nodes": max_nodes,
            "rank_count": len(fused),
            "result_count": len(result_chunks),
        })

        return result_chunks
