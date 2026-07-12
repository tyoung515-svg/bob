"""Pure graph assembler + a read-only live adapter over the memory singletons.

``assemble_memory_graph`` is a PURE function (no I/O, no clock, no writes): it
takes already-fetched fact / conversation / collection / k-NN inputs and emits
the ``{"nodes": [...], "edges": [...], "meta": {...}}`` document. All caps and
the score floor are enforced here, so a fixture can prove them.

``build_graph_from_memory`` is the LIVE adapter: it reads the memory singletons
(``fact_store``, ``event_log``, and the Qdrant client behind the retriever's
provider) using ONLY read operations — ``query``/``replay``/``get_collections``/
``scroll``/``query_points`` — and hands the results to the pure assembler. It
never calls a mutating method (no ``put``/``delete``/``append``/``upsert``/
``create_collection``), so it is safe against the live substrate (inv. 16).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# The generation_method the L1 extractor stamps on every auto-extracted fact
# (mirror of core/memory/extractor.py::_GENERATION_METHOD — read-only). The
# memory browser (api/server.py) lists exactly these; the graph does too.
L1_GENERATION_METHOD = "extract_facts_from_event"

# The L0 event kind that represents a conversation turn (core/nodes/_l0_events.py).
_CONVERSATION_EVENT_KIND = "agent_turn"

NODE_FACT = "fact"
NODE_CONVERSATION = "conversation"

EDGE_PROVENANCE = "provenance"
EDGE_KNN = "knn"

DEFAULT_NODE_CAP = 500
NODE_CAP_MAX = 5000

# k-NN is capped at ~5 neighbours per fact (SPEC §4 / D9) with a score floor so
# only meaningfully-similar facts are linked.
DEFAULT_KNN_K = 5
KNN_K_MAX = 5
DEFAULT_KNN_SCORE_FLOOR = 0.35

_LABEL_MAX = 120
_TEXT_MAX = 500


# ── Models ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    label: str
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "label": self.label, "payload": self.payload}


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    type: str
    weight: float | None = None

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"source": self.source, "target": self.target, "type": self.type}
        if self.weight is not None:
            d["weight"] = self.weight
        return d


# ── Helpers ──────────────────────────────────────────────────────────────────


def _truncate(text: Any, limit: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(f, hi))


def _fact_node_id(fact_id: str) -> str:
    return f"{NODE_FACT}:{fact_id}"


def _conversation_node_id(event_id: str) -> str:
    return f"{NODE_CONVERSATION}:{event_id}"


def _collection_node_id(collection: str, point_id: str) -> str:
    return f"{collection}:{point_id}"


# ── Pure assembler ───────────────────────────────────────────────────────────


def assemble_memory_graph(
    *,
    facts: Iterable[Mapping[str, Any]] = (),
    conversations: Iterable[Mapping[str, Any]] = (),
    collections: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    knn_neighbors: Mapping[str, Iterable[tuple[str, float]]] | None = None,
    node_cap: int = DEFAULT_NODE_CAP,
    knn_k: int = DEFAULT_KNN_K,
    knn_score_floor: float = DEFAULT_KNN_SCORE_FLOOR,
    type_filter: Iterable[str] | None = None,
    warnings: Sequence[str] | None = None,
) -> dict:
    """Assemble the graph document. Pure: no I/O, no writes.

    Node priority order (so the core memory substrate always survives the cap):
    facts → conversations → additional-collection points. The cap binds on the
    TOTAL node count; edges are only kept when both endpoints survive the cap.
    """
    node_cap = _clamp_int(node_cap, DEFAULT_NODE_CAP, 1, NODE_CAP_MAX)
    knn_k = _clamp_int(knn_k, DEFAULT_KNN_K, 1, KNN_K_MAX)
    knn_score_floor = _clamp_float(knn_score_floor, DEFAULT_KNN_SCORE_FLOOR, 0.0, 1.0)
    allowed_types: set[str] | None = set(type_filter) if type_filter is not None else None
    warn: list[str] = list(warnings or [])

    def _type_ok(node_type: str) -> bool:
        return allowed_types is None or node_type in allowed_types

    # --- candidate nodes, in priority order -----------------------------------
    candidates: list[GraphNode] = []
    fact_ids: set[str] = set()  # every fact_id seen (regardless of cap/filter)

    for f in facts:
        fid = f.get("fact_id")
        if not fid:
            continue
        fid = str(fid)
        fact_ids.add(fid)
        if not _type_ok(NODE_FACT):
            continue
        candidates.append(
            GraphNode(
                id=_fact_node_id(fid),
                type=NODE_FACT,
                label=_truncate(f.get("text") or f.get("subject") or fid, _LABEL_MAX),
                payload={
                    "fact_id": fid,
                    "text": _truncate(f.get("text"), _TEXT_MAX),
                    "subject": f.get("subject"),
                    "predicate": f.get("predicate"),
                    "ts": f.get("ts"),
                    "source_event_id": f.get("source_event_id"),
                    "rank": f.get("rank"),
                },
            )
        )

    for c in conversations:
        eid = c.get("event_id") or c.get("id")
        if not eid:
            continue
        eid = str(eid)
        if not _type_ok(NODE_CONVERSATION):
            continue
        label = c.get("user_message") or c.get("assistant_response") or f"turn {c.get('turn_id') or eid}"
        candidates.append(
            GraphNode(
                id=_conversation_node_id(eid),
                type=NODE_CONVERSATION,
                label=_truncate(label, _LABEL_MAX),
                payload={
                    "event_id": eid,
                    "user_message": _truncate(c.get("user_message"), _TEXT_MAX),
                    "assistant_response": _truncate(c.get("assistant_response"), _TEXT_MAX),
                    "face_id": c.get("face_id"),
                    "turn_id": c.get("turn_id"),
                    "ts": c.get("ts"),
                },
            )
        )

    collection_names: list[str] = []
    for cname, points in (collections or {}).items():
        collection_names.append(cname)
        if not _type_ok(cname):
            continue
        for p in points:
            pid = p.get("id") or p.get("point_id")
            if pid is None:
                continue
            pid = str(pid)
            payload = p.get("payload") or {}
            label = (
                payload.get("text")
                or payload.get("chunk_text")
                or payload.get("title")
                or payload.get("source_path")
                or pid
            )
            candidates.append(
                GraphNode(
                    id=_collection_node_id(cname, pid),
                    type=cname,
                    label=_truncate(label, _LABEL_MAX),
                    payload={"point_id": pid, "collection": cname, **_shallow_payload(payload)},
                )
            )

    # --- apply the node cap (priority order preserved) ------------------------
    truncated = len(candidates) > node_cap
    kept = candidates[:node_cap]
    surviving_ids = {n.id for n in kept}

    # --- edges: provenance (fact -> source conversation) ----------------------
    edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def _add_edge(src: str, tgt: str, etype: str, weight: float | None = None) -> None:
        key = (src, tgt, etype)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(GraphEdge(source=src, target=tgt, type=etype, weight=weight))

    for n in kept:
        if n.type != NODE_FACT:
            continue
        src_event = n.payload.get("source_event_id")
        if not src_event:
            continue
        conv_id = _conversation_node_id(str(src_event))
        if conv_id in surviving_ids:
            _add_edge(n.id, conv_id, EDGE_PROVENANCE)

    # --- edges: vector k-NN between fact nodes (cap + score floor) ------------
    knn_pairs_seen: set[frozenset[str]] = set()
    for fid, neighbors in (knn_neighbors or {}).items():
        fid = str(fid)
        src_node = _fact_node_id(fid)
        if src_node not in surviving_ids:
            continue
        # floor first, then keep the top-k by score (the cap that "binds").
        floored = [
            (str(nid), float(score))
            for nid, score in neighbors
            if str(nid) != fid and float(score) >= knn_score_floor
        ]
        floored.sort(key=lambda t: t[1], reverse=True)
        for nid, score in floored[:knn_k]:
            tgt_node = _fact_node_id(nid)
            if tgt_node not in surviving_ids:
                continue
            pair = frozenset((src_node, tgt_node))
            if len(pair) == 1 or pair in knn_pairs_seen:
                continue
            knn_pairs_seen.add(pair)
            _add_edge(src_node, tgt_node, EDGE_KNN, weight=round(score, 6))

    counts_by_type: dict[str, int] = {}
    for n in kept:
        counts_by_type[n.type] = counts_by_type.get(n.type, 0) + 1

    return {
        "nodes": [n.as_dict() for n in kept],
        "edges": [e.as_dict() for e in edges],
        "meta": {
            "node_count": len(kept),
            "edge_count": len(edges),
            "node_cap": node_cap,
            "truncated": truncated,
            "knn_k": knn_k,
            "knn_score_floor": knn_score_floor,
            "collections": sorted(collection_names),
            "counts_by_type": counts_by_type,
            "total_facts": len(fact_ids),
            "warnings": warn,
        },
    }


def _shallow_payload(payload: Mapping[str, Any]) -> dict:
    """A jsonable, size-bounded copy of a collection point's payload."""
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            out[k] = _truncate(v, _TEXT_MAX)
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x for x in list(v)[:20] if isinstance(x, (str, int, float, bool)) or x is None]
        # dicts / other nested structures are dropped to keep the response bounded
    return out


# ── Live read-only adapter ───────────────────────────────────────────────────


def _fact_summary(fact: Any) -> dict:
    body = getattr(fact, "body", None) or {}
    confidence = getattr(fact, "confidence", None)
    return {
        "fact_id": getattr(fact, "fact_id", None),
        "text": body.get("text"),
        "subject": body.get("subject"),
        "predicate": body.get("predicate"),
        "ts": getattr(fact, "ts", None),
        "source_event_id": getattr(fact, "source_event_id", None),
        "rank": getattr(confidence, "rank", None),
    }


def _conversation_summary(event: Any) -> dict:
    body = getattr(event, "body", None) or {}
    return {
        "event_id": getattr(event, "event_id", None),
        "user_message": body.get("user_message"),
        "assistant_response": body.get("assistant_response"),
        "face_id": body.get("face_id"),
        "turn_id": body.get("turn_id"),
        "ts": getattr(event, "ts", None),
    }


def _resolve_qdrant(mem: Any) -> tuple[Any, str]:
    """Pull the READ-ONLY Qdrant client + collection prefix off the singletons.

    The client is reached through the retriever's provider. Everything called on
    it downstream is a read (get_collections / scroll / query_points).
    """
    provider = getattr(getattr(mem, "retriever", None), "_provider", None)
    client = getattr(provider, "_client", None)
    prefix = getattr(provider, "collection_prefix", "bobclaw_")
    return client, prefix


def _scroll_all(client: Any, collection: str, *, limit: int, with_vectors: bool) -> list[Any]:
    """Read points from *collection* up to *limit* (read-only ``scroll``)."""
    points: list[Any] = []
    next_offset = None
    while len(points) < limit:
        batch, next_offset = client.scroll(
            collection_name=collection,
            limit=min(256, limit - len(points)),
            with_payload=True,
            with_vectors=with_vectors,
            offset=next_offset,
        )
        points.extend(batch)
        if not next_offset or not batch:
            break
    return points[:limit]


def _build_knn(
    client: Any,
    memory_collections: Sequence[str],
    knn_k: int,
    fact_budget: int,
) -> dict[str, list[tuple[str, float]]]:
    """Build ``fact_id -> [(neighbour_fact_id, score), ...]`` via read-only Qdrant.

    Scrolls each memory collection (with vectors), then for every point that
    carries a ``source_fact_id`` runs a k-NN ``query_points`` and maps the
    neighbours back to their ``source_fact_id``. The score floor + hard k cap are
    applied by the pure assembler, so we over-fetch ``knn_k + 1`` here (to drop
    the self hit) and let the assembler be the authority.
    """
    neighbors: dict[str, list[tuple[str, float]]] = {}
    processed = 0
    for coll in memory_collections:
        points = _scroll_all(client, coll, limit=fact_budget, with_vectors=True)
        # point_id -> source_fact_id, for mapping neighbour hits back to facts.
        for pt in points:
            if processed >= fact_budget:
                break
            payload = getattr(pt, "payload", None) or {}
            fid = payload.get("source_fact_id")
            vector = getattr(pt, "vector", None)
            if not fid or vector is None:
                continue
            processed += 1
            try:
                hits = client.query_points(
                    collection_name=coll,
                    query=vector,
                    limit=knn_k + 1,
                ).points
            except Exception as exc:  # noqa: BLE001 — one bad query must not sink the graph
                log.warning("memory_graph: knn query failed on %s: %s", coll, exc)
                continue
            bucket = neighbors.setdefault(str(fid), [])
            for h in hits:
                h_payload = getattr(h, "payload", None) or {}
                n_fid = h_payload.get("source_fact_id")
                if not n_fid or str(n_fid) == str(fid):
                    continue
                bucket.append((str(n_fid), float(getattr(h, "score", 0.0))))
    return neighbors


async def build_graph_from_memory(
    mem: Any,
    *,
    node_cap: int = DEFAULT_NODE_CAP,
    knn_k: int = DEFAULT_KNN_K,
    knn_score_floor: float = DEFAULT_KNN_SCORE_FLOOR,
    type_filter: Iterable[str] | None = None,
) -> dict:
    """Assemble the graph from the live memory singletons (READ-ONLY)."""
    node_cap = _clamp_int(node_cap, DEFAULT_NODE_CAP, 1, NODE_CAP_MAX)
    knn_k = _clamp_int(knn_k, DEFAULT_KNN_K, 1, KNN_K_MAX)
    warnings: list[str] = []

    # --- L1 facts -------------------------------------------------------------
    facts: list[dict] = []
    try:
        raw_facts = await mem.fact_store.query({"generation_method": L1_GENERATION_METHOD})
        facts = [_fact_summary(f) for f in raw_facts]
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"facts: {type(exc).__name__}: {exc}"[:160])

    # --- L0 conversation events ----------------------------------------------
    conversations: list[dict] = []
    try:
        count = 0
        async for ev in mem.event_log.replay():
            if getattr(ev, "kind", None) != _CONVERSATION_EVENT_KIND:
                continue
            conversations.append(_conversation_summary(ev))
            count += 1
            if count >= node_cap:
                break
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"conversations: {type(exc).__name__}: {exc}"[:160])

    # --- additional live Qdrant collections + fact k-NN -----------------------
    collections: dict[str, list[dict]] = {}
    knn_neighbors: dict[str, list[tuple[str, float]]] = {}
    client, prefix = _resolve_qdrant(mem)
    if client is not None:
        try:
            coll_names = [c.name for c in client.get_collections().collections]
        except Exception as exc:  # noqa: BLE001
            coll_names = []
            warnings.append(f"collections: {type(exc).__name__}: {exc}"[:160])

        memory_collections = [c for c in coll_names if c.startswith(prefix)]
        foreign = [c for c in coll_names if not c.startswith(prefix)]

        for cname in foreign:
            try:
                pts = _scroll_all(client, cname, limit=node_cap, with_vectors=False)
                collections[cname] = [
                    {"id": str(getattr(p, "id", "")), "payload": getattr(p, "payload", None) or {}}
                    for p in pts
                ]
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"collection {cname}: {type(exc).__name__}: {exc}"[:160])

        try:
            knn_neighbors = _build_knn(client, memory_collections, knn_k, fact_budget=node_cap)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"knn: {type(exc).__name__}: {exc}"[:160])

    return assemble_memory_graph(
        facts=facts,
        conversations=conversations,
        collections=collections,
        knn_neighbors=knn_neighbors,
        node_cap=node_cap,
        knn_k=knn_k,
        knn_score_floor=knn_score_floor,
        type_filter=type_filter,
        warnings=warnings,
    )
