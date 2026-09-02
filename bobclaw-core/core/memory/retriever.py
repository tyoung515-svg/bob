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

# R4 — bounded backfill past dangling/forgotten/deprecated hits.
# The provider returns vector hits in score order; some point at facts that were
# forgotten (DELETE /api/memory/facts) or deprecated. If we only ever fetch
# `top_k`, a run of high-ranked dangling hits truncates recall below `top_k` even
# when valid lower-ranked facts exist. So overfetch and page in bounded batches,
# skipping dangling hits, until we collect `top_k` valid results, the provider is
# exhausted, or we hit the documented safety bound below.
_RECALL_CANDIDATE_CAP = 200  # safety bound: max vector hits scanned per ranking

# The production embedder has a 2048-token context.  Recall is a semantic hint,
# not the model prompt, so keep a conservative character budget (roughly 768
# English tokens) and preserve both ends of long messages: task framing is often
# at the front while the actual question is commonly at the end.
_RECALL_QUERY_CHAR_BUDGET = 3072
_RECALL_QUERY_TRUNCATION_MARKER = "\n[... recall query truncated ...]\n"


def _bounded_recall_query(query: str) -> str:
    """Return an embedder-safe recall query without mutating chat state."""
    if len(query) <= _RECALL_QUERY_CHAR_BUDGET:
        return query
    content_budget = (
        _RECALL_QUERY_CHAR_BUDGET - len(_RECALL_QUERY_TRUNCATION_MARKER)
    )
    head_chars = content_budget // 2
    tail_chars = content_budget - head_chars
    return query[:head_chars] + _RECALL_QUERY_TRUNCATION_MARKER + query[-tail_chars:]


def _recall_batch_size(top_k: int) -> int:
    """Per-page provider limit: over-fetch headroom so dangling hits can be
    skipped and still backfill up to `top_k` valid facts in a single page for the
    common case."""
    return min(max(top_k * 4, top_k + 10), _RECALL_CANDIDATE_CAP)


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
        t0_recall_enabled=False,
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
        self._t0_recall_enabled = bool(t0_recall_enabled)

    async def search(
        self,
        query: str,
        top_k: int = 3,
        threshold: float = 0.35,
        filters: dict | None = None,
        hop_budget: int = 1,
    ) -> list[RetrievedChunk]:
        query = _bounded_recall_query(query)
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

        query_vec = (await self._embedder.embed([query]))[0]

        if hop_budget == 2:
            return await self._search_hop2(
                query, query_vec, top_k, threshold,
                provider_filters or None, include_deprecated,
            )
        return await self._search_hop1(
            query, query_vec, top_k, threshold,
            provider_filters or None, include_deprecated,
        )

    def _iter_ranking_hits(self, query_vec, provider_filters, batch):
        """Yield provider hits in score order, paging in bounded batches and
        deduping by id, up to a HARD total of ``_RECALL_CANDIDATE_CAP`` rows.

        LAZY: the caller stops the generator the instant it has ``top_k`` valid
        facts, so a healthy store (few/no dangling hits) is NOT drained on every
        recall — only the dangling-backfill case pages deeper. The per-request
        limit is clamped to the remaining budget, so total rows scanned never
        exceeds the cap (not cap + batch).
        """
        seen: set[str] = set()
        offset = 0
        scanned = 0
        while scanned < _RECALL_CANDIDATE_CAP:
            limit = min(batch, _RECALL_CANDIDATE_CAP - scanned)
            results = self._provider.query_vector(
                self._store_id, query_vec, limit, provider_filters, offset=offset,
            )
            page = list(results.hits)
            if not page:
                return
            fresh = [h for h in page if h.id not in seen]
            if not fresh:
                return  # provider ignored offset / only repeats — stop
            for h in fresh:
                seen.add(h.id)
                scanned += 1
                yield h
                if scanned >= _RECALL_CANDIDATE_CAP:
                    return
            if len(page) < limit:
                return  # provider exhausted
            offset += len(page)

    async def _classify_hit(self, hit, rrf_score: float, include_deprecated: bool):
        """Validate one ranked hit against the FactStore and apply the read-time
        credibility decay. Returns ``(chunk|None, kind)`` with kind in
        ``{"valid","dangling","deprecated"}``. ``dangling`` = the fact was forgotten
        but a vector lingers (fail open, backfill from the next hit)."""
        if (
            hit.payload.get("projection_task") == "project_verbatim"
            and not self._t0_recall_enabled
        ):
            # Writer and read pins are intentionally independent.  Backfill past
            # gated T0 hits so their presence cannot suppress legacy L1 facts.
            return None, "t0_disabled"
        source_fact_id: str | None = hit.payload.get("source_fact_id")
        boosted = rrf_score
        if source_fact_id:
            try:
                fact = await self._fact_store.get(source_fact_id)
            except L1ValidationFailed:
                log.warning(
                    "recall: skipping hit %s — fact %s not in FactStore "
                    "(forgotten or dangling vector)",
                    hit.id, source_fact_id,
                )
                return None, "dangling"
            # Decay is applied at READ time (Hard Rule 15) — never written back.
            cred_mean = credibility_mean(fact.confidence, datetime.now(timezone.utc))
            boosted = rrf_score * max(0.05, cred_mean)
            if not include_deprecated and fact.confidence.rank == "deprecated":
                return None, "deprecated"
        return (
            RetrievedChunk(
                content=hit.payload.get("text", ""),
                score=rrf_score,
                source_fact_id=source_fact_id,
                source_path=hit.payload.get("source_path"),
                heading_path=hit.payload.get("heading_path", []),
                boosted_score=boosted,
            ),
            "valid",
        )

    async def _search_hop1(
        self, query, query_vec, top_k, threshold, provider_filters, include_deprecated,
    ) -> list[RetrievedChunk]:
        """Single-ranking recall with LAZY backfill: page the provider only until
        ``top_k`` VALID facts are collected (skipping dangling/deprecated), the
        provider is exhausted, or the safety cap is hit. The RRF score for a single
        ranking is ``1/(60+rank+1)`` — identical to ``rrf_fuse`` — and a dangling
        hit still consumes its rank (matches the pre-lazy behaviour)."""
        batch = _recall_batch_size(top_k)
        result_chunks: list[RetrievedChunk] = []
        dangling_skipped = deprecated_skipped = 0
        kept_rank = 0
        scanned = 0
        for hit in self._iter_ranking_hits(query_vec, provider_filters, batch):
            scanned += 1
            if hit.score < threshold:
                continue  # pre-fusion threshold: no rank, not collected
            rrf_score = 1.0 / (60 + kept_rank + 1)
            kept_rank += 1
            chunk, kind = await self._classify_hit(hit, rrf_score, include_deprecated)
            if kind == "dangling":
                dangling_skipped += 1
                continue
            if kind == "deprecated":
                deprecated_skipped += 1
                continue
            if chunk is None:
                continue
            result_chunks.append(chunk)
            if len(result_chunks) >= top_k:
                break

        result_chunks.sort(key=lambda r: r.boosted_score or 0, reverse=True)
        result_chunks = result_chunks[:top_k]
        self._log_recall(
            query, top_k, 1, 1, scanned,
            dangling_skipped, deprecated_skipped, len(result_chunks),
        )
        return result_chunks

    async def _search_hop2(
        self, query, query_vec, top_k, threshold, provider_filters, include_deprecated,
    ) -> list[RetrievedChunk]:
        """Two-hop (wikilink-expanded) recall: RRF-fuse the root ranking with a
        bounded follow-up query per wikilink, then validate/backfill. Each ranking
        is one bounded overfetch (not paged to the cap) — the deep dangling-backfill
        paging is the hop-1 hot path; hop-2 stays bounded."""
        batch = _recall_batch_size(top_k)
        initial = list(
            self._provider.query_vector(
                self._store_id, query_vec, batch, provider_filters,
            ).hits
        )
        wikilinks: set[str] = set()
        for hit in initial:
            for wl in hit.payload.get("wikilinks", []):
                wikilinks.add(wl)
        rankings: list[list] = [initial]
        for wl_target in wikilinks:
            wl_vec = (await self._embedder.embed([wl_target]))[0]
            rankings.append(
                list(
                    self._provider.query_vector(
                        self._store_id, wl_vec, batch, provider_filters,
                    ).hits
                )
            )

        filtered = [[h for h in r if h.score >= threshold] for r in rankings]
        fused = rrf_fuse(filtered, k=60)

        result_chunks: list[RetrievedChunk] = []
        dangling_skipped = deprecated_skipped = 0
        seen_ids: set[str] = set()
        for hit in fused:
            if len(result_chunks) >= top_k:
                break
            if hit.id in seen_ids:
                continue
            seen_ids.add(hit.id)
            chunk, kind = await self._classify_hit(hit, hit.score, include_deprecated)
            if kind == "dangling":
                dangling_skipped += 1
                continue
            if kind == "deprecated":
                deprecated_skipped += 1
                continue
            if chunk is None:
                continue
            result_chunks.append(chunk)

        result_chunks.sort(key=lambda r: r.boosted_score or 0, reverse=True)
        result_chunks = result_chunks[:top_k]
        self._log_recall(
            query, top_k, 2, len(wikilinks) + 1, len(fused),
            dangling_skipped, deprecated_skipped, len(result_chunks),
        )
        return result_chunks

    def _log_recall(self, query, top_k, hop_budget, max_nodes, candidate_count,
                    dangling_skipped, deprecated_skipped, result_count) -> None:
        # Metric (counts only — never fact contents): a high dangling rate flags a
        # forget/index-drift problem worth cleaning up.
        if dangling_skipped or deprecated_skipped:
            log.info(
                "recall: backfilled past %d dangling + %d deprecated hit(s) over "
                "%d candidates for k=%d",
                dangling_skipped, deprecated_skipped, candidate_count, top_k,
            )
        self._query_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "k": top_k,
            "hop_budget": hop_budget,
            "max_nodes": max_nodes,
            "rank_count": candidate_count,
            "candidate_count": candidate_count,
            "dangling_skipped": dangling_skipped,
            "deprecated_skipped": deprecated_skipped,
            "result_count": result_count,
        })
