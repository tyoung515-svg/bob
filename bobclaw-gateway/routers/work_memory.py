"""Work-lane memory router — the SPS lane, server-enforced.

The lane is one self-contained unit (exportable, then deletable wholesale):
  collection : sps__2560 on the box Qdrant (127.0.0.1:56333)
  ledger     : <repo>/ledger/claims/work_notes.jsonl  (append-only, hash-chained)
  principal  : sps (the ONLY writer; every other principal stays read-only)

Authorization comes from the AGENT TOKEN's scope, never from anything the client
sends:
  * work scope  — may_touch matches the lane root      -> read + write
  * personal    — may_touch matches the owner's personal lane -> READ ONLY
  * anything else                                      -> 403

That is the whole boundary: personal memory can look into work, work can never
look into personal. The work token's scope carries no family-lane path, so the
sibling /agent-memory/* router refuses it independently (defence in depth).

Transport cost: search embeds the query through the box embedder (single-slot
llama.cpp at 127.0.0.1:58081) — the same slot live BoB recall uses, so calls are
serialised. Keep limits small.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from aiohttp import web

from core.permissions import Scope, evaluate_path
from core.paths import family_memory_path, sps_lane_root

router = web.RouteTableDef()

ROOT = sps_lane_root()
QDRANT = os.environ.get("SPS_QDRANT_URL", "http://127.0.0.1:56333")
EMBED_URL = os.environ.get("SPS_EMBED_URL", "http://127.0.0.1:58081")
EMBED_MODEL = os.environ.get("SPS_EMBED_MODEL", "qwen3-embedding-4b")
DIM = 2560
MAX_CONTENT = 12000
MAX_QUERY = 1000
CATEGORIES = ("fact", "preference", "project", "note")

LANE = {
    "name": "sps",
    "root": str(ROOT) if ROOT is not None else None,
    "collection": os.environ.get("SPS_COLLECTION", "sps__2560"),
    "ledger": ROOT / "ledger" / "claims" / "work_notes.jsonl" if ROOT is not None else None,
    "docs": ROOT / "docs" if ROOT is not None else None,
    "owner": "sps",
    "readers": ["bobclaw", "sps"],
}

PERSONAL_READ_PATH = family_memory_path()

_TEXT_EXTS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".htm", ".rtf", ".log"}


# ─────────────────────────────── transport ────────────────────────────────

def _req(url: str, payload: dict | None = None, method: str = "POST", timeout: float = 30.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def _embed(text: str) -> list[float]:
    r = _req(f"{EMBED_URL}/v1/embeddings", {"model": EMBED_MODEL, "input": text}, timeout=60)
    vecs = [d["embedding"] for d in sorted(r["data"], key=lambda d: d.get("index", 0))]
    if len(vecs) != 1:
        raise RuntimeError(f"embedder returned {len(vecs)} vectors for 1 input")
    vec = vecs[0]
    if len(vec) != DIM:
        raise RuntimeError(f"embedding dim {len(vec)} != {DIM} — refusing to write")
    if not any(abs(x) > 1e-9 for x in vec):
        raise RuntimeError("embedder returned a zero vector — refusing to write")
    return vec


# ──────────────────────────────── auth ────────────────────────────────────

def _claims(request: web.Request) -> dict:
    claims = request.get("user")
    return claims if isinstance(claims, dict) else {}


def _scope(request: web.Request) -> Scope | None:
    raw = _claims(request).get("scope")
    if not isinstance(raw, dict):
        return None
    try:
        return Scope.model_validate(raw)
    except Exception:
        return None


def _auto(request: web.Request, path: str) -> bool:
    scope = _scope(request)
    return scope is not None and evaluate_path(path, scope) == "auto"


def lane_of(request: web.Request) -> str | None:
    """The lane this token owns for WRITE, or None. Matched on the lane root only."""
    return LANE["name"] if LANE["root"] and _auto(request, LANE["root"] + "/work") else None


def can_write(request: web.Request) -> bool:
    return lane_of(request) == LANE["name"]


def can_read(request: web.Request) -> bool:
    """Personal read is allowed; write never is."""
    return bool(LANE["root"]) and (can_write(request) or (
        PERSONAL_READ_PATH is not None and _auto(request, str(PERSONAL_READ_PATH))))


def _require_configured() -> None:
    if not LANE["root"]:
        raise web.HTTPServiceUnavailable(text="Work memory is unavailable: set BOB_SPS_LANE_ROOT.")


def _require_read(request: web.Request) -> None:
    _require_configured()
    if not can_read(request):
        raise web.HTTPForbidden(text="lane_read_scope_required")


def _require_write(request: web.Request) -> None:
    _require_configured()
    if not can_write(request):
        raise web.HTTPForbidden(text="lane_write_scope_required")


# ─────────────────────────────── ledger ───────────────────────────────────

def _ledger_entries() -> list[dict]:
    path = LANE["ledger"]
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a torn tail is evidence, not a crash: keep what parses
    return out


def _append_entry(content: str, category: str) -> dict:
    path = LANE["ledger"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
            prev = json.loads(lines[-1]).get("hash", "") if lines else ""
            entry = {
                "note_id": uuid.uuid4().hex,
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "owner": LANE["owner"],
                "lane": LANE["name"],
                "category": category,
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "prev_hash": prev,
            }
            canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            entry["hash"] = hashlib.sha256((prev + canonical).encode("utf-8")).hexdigest()
            line = json.dumps(entry, ensure_ascii=False)
            f.seek(0, 2)
            if f.tell() and not line.endswith("\n"):
                pass
            f.write(("\n" if f.tell() else "") + line + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return entry


def _find_note(note_id: str) -> dict | None:
    for e in reversed(_ledger_entries()):
        if e.get("note_id") == note_id:
            return e
    return None


# ──────────────────────────────── qdrant ──────────────────────────────────

def _point_id(note_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sps-work-note:{note_id}"))


def _upsert_note(entry: dict) -> str:
    vec = _embed(entry["content"])
    point_id = _point_id(entry["note_id"])
    payload = {
        "chunk_text": entry["content"],
        "chunk_id": point_id,
        "chunk_hash": entry["content_sha256"],
        "source_path": "ledger/claims/work_notes.jsonl",
        "title": f"work note ({entry['category']})",
        "corpus": "sps",
        "source_kind": "work_note",
        "owner": LANE["owner"],
        "note_id": entry["note_id"],
        "created_at": entry["ts"],
    }
    _req(f"{QDRANT}/collections/{LANE['collection']}/points?wait=true",
         {"points": [{"id": point_id, "vector": vec, "payload": payload}]}, method="PUT")
    return point_id


def _search_points(vector: list[float], limit: int) -> list[dict]:
    r = _req(f"{QDRANT}/collections/{LANE['collection']}/points/search",
             {"vector": vector, "limit": limit, "with_payload": True}, timeout=30)
    return r.get("result", [])


def _get_point(point_id: str) -> dict | None:
    try:
        uuid.UUID(point_id)                 # Qdrant ids here are UUIDs; junk is simply absent
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        q = urllib.parse.quote(point_id, safe="")
        r = _req(f"{QDRANT}/collections/{LANE['collection']}/points/{q}", method="GET")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        raise
    return r.get("result")


# ──────────────────────────── literal search ──────────────────────────────

def _literal_notes(query: str) -> list[dict]:
    needle = query.casefold()
    hits = []
    for e in reversed(_ledger_entries()):
        if needle in str(e.get("content", "")).casefold():
            hits.append({
                "source_id": f"sps:note:{e['note_id']}",
                "kind": "work_note",
                "title": f"work note ({e.get('category')})",
                "score": None,
                "excerpt": str(e.get("content", ""))[:600],
                "truncated": len(str(e.get("content", ""))) > 600,
                "saved_at": e.get("ts"),
            })
    return hits


def _literal_docs(query: str, limit: int = 5) -> list[dict]:
    """Bounded literal scan of the corpus working tree (text files only)."""
    needle = query.casefold()
    docs = LANE["docs"]
    if not docs.is_dir():
        return []
    budget = 4000                      # files inspected, keeps the call cheap
    hits = []
    for path in docs.rglob("*"):
        budget -= 1
        if budget <= 0 or len(hits) >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in _TEXT_EXTS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if needle not in text.casefold():
            continue
        idx = text.casefold().find(needle)
        start = max(0, idx - 200)
        hits.append({
            "source_id": "sps:doc:" + str(path.relative_to(docs)),
            "kind": "corpus_doc",
            "title": path.name,
            "score": None,
            "excerpt": text[start:start + 600],
            "truncated": len(text) > 600,
        })
    return hits


# ──────────────────────────────── routes ──────────────────────────────────

def _int(body: dict, key: str, default: int, maximum: int) -> int:
    v = body.get(key, default)
    if type(v) is not int or v < 0 or v > maximum:
        raise web.HTTPBadRequest(text=f"invalid_{key}")
    return v


async def _body(request: web.Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid_json")
    if not isinstance(b, dict):
        raise web.HTTPBadRequest(text="invalid_json")
    return b


def _coverage(request: web.Request) -> dict:
    return {
        "lane": LANE["name"],
        "collections": [LANE["collection"]],
        "writer": LANE["owner"],
        "caller_can_write": can_write(request),
        "complete_corpus": False,
        "search_mode": "semantic+literal",
        "excluded": ["every other Qdrant collection", "personal/family memory",
                     "unindexed files outside the lane repo"],
    }


@router.get("/work-memory/status")
async def status(request: web.Request) -> web.Response:
    _require_read(request)
    info = await _run(lambda: _req(f"{QDRANT}/collections/{LANE['collection']}", method="GET"))
    result = info.get("result", {})
    notes = len(_ledger_entries())
    return web.json_response({
        "lane": LANE["name"],
        "collection": LANE["collection"],
        "points_count": result.get("points_count"),
        "indexed_vectors_count": result.get("indexed_vectors_count"),
        "status": result.get("status"),
        "work_notes": notes,
        "owner": LANE["owner"],
        "readers": LANE["readers"],
        "caller_can_write": can_write(request),
        "coverage": _coverage(request),
    })


@router.post("/work-memory/search")
async def search(request: web.Request) -> web.Response:
    _require_read(request)
    b = await _body(request)
    q = b.get("query", "")
    if not isinstance(q, str) or len(q) > MAX_QUERY:
        raise web.HTTPBadRequest(text="invalid_query")
    limit = _int(b, "limit", 10, 50)
    offset = _int(b, "offset", 0, 100000)
    if limit == 0:
        raise web.HTTPBadRequest(text="invalid_limit")

    # Relevance floor. The box embedder's cosine baseline is high (~0.58 for an
    # unrelated query), so an unfiltered ANN search always returns "neighbours"
    # that look like hits. Nothing is dropped silently: the count is reported.
    # A no-whitespace identifier-like query (a hash, key, id) is treated as an
    # exact-token probe and needs a much higher score, so probing for a value
    # that lives outside this lane returns zero hits instead of noise.
    min_score = float(os.environ.get("SPS_MIN_SCORE", "0.55"))
    if " " not in q.strip() and len(q.strip()) >= 12:
        min_score = max(min_score, 0.75)

    items: list[dict] = []
    filtered = 0
    embed_error = None
    if q.strip():
        try:
            points = await _run(lambda: _search_points(_embed(q), min(limit + offset, 50)))
        except Exception as exc:
            points = []
            embed_error = f"{type(exc).__name__}: {exc}"
        for p in points:
            score = p.get("score")
            if score is not None and score < min_score:
                filtered += 1
                continue
            payload = p.get("payload") or {}
            pid = str(p.get("id"))
            is_note = payload.get("source_kind") == "work_note" and payload.get("note_id")
            text = str(payload.get("chunk_text", ""))
            items.append({
                "source_id": f"sps:note:{payload['note_id']}" if is_note else f"sps:chunk:{pid}",
                "kind": "work_note" if is_note else "corpus_chunk",
                "title": payload.get("title") or payload.get("source_path") or "",
                "source_path": payload.get("source_path"),
                "score": score,
                "excerpt": text[:600],
                "truncated": len(text) > 600,
            })
        # literal work-note hits (exact strings), then a bounded doc scan
        literal = _literal_notes(q)
        seen = {i["source_id"] for i in items}
        items.extend([h for h in literal if h["source_id"] not in seen])
        if len(items) < limit + offset:
            seen = {i["source_id"] for i in items}
            items.extend([h for h in _literal_docs(q, limit=limit) if h["source_id"] not in seen])

    page = items[offset:offset + limit]
    has_more = offset + limit < len(items)
    return web.json_response({
        "items": page,
        "matched_records": len(items),
        "low_relevance_filtered": filtered,
        "min_score": min_score,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "embed_error": embed_error,
        "coverage": _coverage(request),
    })


@router.post("/work-memory/read")
async def read(request: web.Request) -> web.Response:
    _require_read(request)
    b = await _body(request)
    source_id = b.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise web.HTTPBadRequest(text="invalid_source_id")
    offset = _int(b, "offset", 0, 10000000)
    size = _int(b, "max_chars", 6000, 12000)
    if size == 0:
        raise web.HTTPBadRequest(text="invalid_max_chars")

    if source_id.startswith("sps:note:"):
        entry = await _run(lambda: _find_note(source_id.split(":", 2)[2]))
        if not entry:
            raise web.HTTPNotFound(text="source_not_found")
        text = str(entry.get("content", ""))
        prov = {"note_id": entry.get("note_id"), "ts": entry.get("ts"),
                "category": entry.get("category"), "owner": entry.get("owner"),
                "ledger": str(LANE["ledger"]), "hash": entry.get("hash")}
    elif source_id.startswith("sps:chunk:"):
        pid = source_id.split(":", 2)[2]
        point = await _run(lambda: _get_point(pid))
        if not point:
            raise web.HTTPNotFound(text="source_not_found")
        payload = point.get("payload") or {}
        text = str(payload.get("chunk_text", ""))
        prov = {"point_id": pid, "source_path": payload.get("source_path"),
                "chunk_hash": payload.get("chunk_hash"), "doc_hash": payload.get("doc_hash")}
    elif source_id.startswith("sps:doc:"):
        rel = source_id.split(":", 2)[2]
        docs = LANE["docs"].resolve()
        target = (docs / rel).resolve()
        if not target.is_relative_to(docs) or not target.is_file():
            raise web.HTTPNotFound(text="source_not_found")
        text = await _run(lambda: target.read_text(encoding="utf-8", errors="ignore"))
        prov = {"source_path": rel, "bytes": target.stat().st_size}
    else:
        # A foreign scheme (family:, event:, …) does not exist in this lane.
        raise web.HTTPNotFound(text="source_not_found")

    return web.json_response({
        "source_id": source_id,
        "content": text[offset:offset + size],
        "total_chars": len(text),
        "has_more": offset + size < len(text),
        "next_offset": offset + size if offset + size < len(text) else None,
        "provenance": prov,
    })


@router.post("/work-memory/save")
async def save(request: web.Request) -> web.Response:
    _require_write(request)
    b = await _body(request)
    text = b.get("content")
    category = b.get("category", "fact")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CONTENT:
        raise web.HTTPBadRequest(text="invalid_memory")
    if category not in CATEGORIES:
        raise web.HTTPBadRequest(text="invalid_category")

    entry = await _run(lambda: _append_entry(text.strip(), category))
    try:
        point_id = await _run(lambda: _upsert_note(entry))
        indexed = True
        note = None
    except Exception as exc:                     # ledger is durable; the embed is retryable
        point_id = None
        indexed = False
        note = f"ledger written, embedding/upsert failed: {type(exc).__name__}: {exc}"

    return web.json_response({
        "source_id": f"sps:note:{entry['note_id']}",
        "note_id": entry["note_id"],
        "point_id": point_id,
        "owner": LANE["owner"],
        "lane": LANE["name"],
        "category": category,
        "indexed": indexed,
        "warning": note,
        "provenance": {"ledger": str(LANE["ledger"]), "hash": entry["hash"],
                       "ts": entry["ts"], "authority": "server"},
    }, status=201)


async def _run(fn):
    import asyncio
    return await asyncio.to_thread(fn)
