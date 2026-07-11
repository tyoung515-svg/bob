"""L-EVAL retrieval ceiling harness for the repository-local technical corpus.

The corpus builder is deterministic and follows the lane source policy. Retrieval
is exact brute-force cosine over NumPy arrays so this measures the embedding
ceiling, not a vector-store implementation.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import math
import re
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVAL_SET = Path(__file__).with_name("eval_set.jsonl")
MIN_CHUNK_CHARS = 300
MAX_CHUNK_CHARS = 800
PACK_CHUNK_CHARS = 760
ROOT_MARKDOWN_FILES = (
    "README.md",
    "AGENTS-SETUP.md",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "COMPLIANCE.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
)
CONTENT_TOKEN_RE = re.compile(r"[a-z0-9]+")
CONTENT_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for
    from further had has have having he her here hers herself him himself his how i if in
    into is it its itself just me more most my myself no nor not now of off on once only or
    other our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your yours
    yourself yourselves
    """.split()
)


@dataclass(frozen=True)
class CorpusChunk:
    chunk_id: str
    source: str
    ordinal: int
    text: str


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _clean_text(text: str) -> str:
    text = textwrap.dedent(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _env_comments(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("#")
    )


def _module_docstring(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return ""
    return ast.get_docstring(tree, clean=True) or ""


def iter_sources(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """Return allowlisted source texts in stable path order."""
    sources: list[tuple[str, str]] = []
    for relative_path in ROOT_MARKDOWN_FILES:
        path = repo_root / relative_path
        if path.is_file():
            sources.append((relative_path, path.read_text(encoding="utf-8")))

    for directory in (repo_root / "docs", repo_root / "bobclaw-core" / "docs"):
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
                    sources.append((_relative(path), path.read_text(encoding="utf-8")))

    env_example = repo_root / ".secrets" / "bobclaw.env.example"
    if env_example.is_file():
        sources.append((".secrets/bobclaw.env.example", _env_comments(env_example)))

    app_root = repo_root / "bobclaw-core"
    for path in sorted(app_root.rglob("*.py")):
        relative_path = _relative(path)
        if "/tests/" in f"/{relative_path}" or "/evals/" in f"/{relative_path}":
            continue
        docstring = _module_docstring(path)
        if docstring:
            sources.append((relative_path, docstring))
    return sources


def _split_long_text(text: str) -> list[str]:
    words = text.split()
    parts: list[str] = []
    current: list[str] = []
    current_chars = 0
    for word in words:
        proposed = current_chars + (1 if current else 0) + len(word)
        if current and proposed > PACK_CHUNK_CHARS:
            parts.append(" ".join(current))
            current = [word]
            current_chars = len(word)
        else:
            current.append(word)
            current_chars = proposed
    if current:
        parts.append(" ".join(current))
    return parts


def _pack_source(text: str) -> list[str]:
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n", _clean_text(text))
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > PACK_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_text(paragraph))
            continue
        proposed = f"{current} {paragraph}".strip() if current else paragraph
        if current and len(proposed) > PACK_CHUNK_CHARS:
            chunks.append(current)
            current = paragraph
        else:
            current = proposed
    if current:
        chunks.append(current)

    index = 0
    while index < len(chunks):
        if len(chunks[index]) >= MIN_CHUNK_CHARS or len(chunks) == 1:
            index += 1
            continue
        if index and len(chunks[index - 1]) + 1 + len(chunks[index]) <= MAX_CHUNK_CHARS:
            chunks[index - 1] = f"{chunks[index - 1]} {chunks.pop(index)}"
            continue
        if (
            index + 1 < len(chunks)
            and len(chunks[index]) + 1 + len(chunks[index + 1]) <= MAX_CHUNK_CHARS
        ):
            chunks[index : index + 2] = [f"{chunks[index]} {chunks[index + 1]}"]
            continue
        index += 1
    return chunks


def build_corpus(repo_root: Path = REPO_ROOT) -> list[CorpusChunk]:
    """Build stable source-stem-NNN chunks from the allowlisted corpus."""
    chunks: list[CorpusChunk] = []
    for source, text in iter_sources(repo_root):
        source_key = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
        for ordinal, chunk_text in enumerate(_pack_source(text), start=1):
            chunks.append(
                CorpusChunk(
                    chunk_id=f"{source_key}-{ordinal:03d}",
                    source=source,
                    ordinal=ordinal,
                    text=chunk_text,
                )
            )
    return chunks


def content_tokens(text: str) -> set[str]:
    """Return lowercase non-stopword tokens used by the leakage guard."""
    return {
        token
        for token in CONTENT_TOKEN_RE.findall(text.casefold())
        if token not in CONTENT_STOPWORDS and len(token) > 1
    }


def load_eval_set(path: Path = DEFAULT_EVAL_SET) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        missing = {"id", "query", "relevant_chunk_id", "distractor_chunk_ids"} - row.keys()
        if missing:
            raise ValueError(f"{path}:{line_number}: missing fields: {sorted(missing)}")
        if not isinstance(row["distractor_chunk_ids"], list):
            raise ValueError(
                f"{path}:{line_number}: distractor_chunk_ids must be a list"
            )
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: eval set is empty")
    return rows


def validate_eval_set(rows: list[dict[str, Any]], corpus: list[CorpusChunk]) -> None:
    chunk_by_id = {chunk.chunk_id: chunk for chunk in corpus}
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    for index, row in enumerate(rows, start=1):
        eval_id = row["id"]
        query = row["query"]
        relevant = row["relevant_chunk_id"]
        distractors = row["distractor_chunk_ids"]
        if not isinstance(eval_id, str) or not eval_id.strip():
            raise ValueError(f"eval row {index}: id must be non-empty text")
        if eval_id in seen_ids:
            raise ValueError(f"eval row {index}: duplicate id {eval_id!r}")
        seen_ids.add(eval_id)
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"eval row {index}: query must be non-empty text")
        if query in seen_queries:
            raise ValueError(f"eval row {index}: duplicate query")
        seen_queries.add(query)
        if relevant not in chunk_by_id:
            raise ValueError(f"eval row {index}: unknown relevant chunk {relevant!r}")
        if not 2 <= len(distractors) <= 3:
            raise ValueError(f"eval row {index}: expected 2-3 distractors")
        if len(set(distractors)) != len(distractors):
            raise ValueError(f"eval row {index}: duplicate distractor")
        if relevant in distractors:
            raise ValueError(
                f"eval row {index}: relevant chunk also listed as distractor"
            )
        unknown = set(distractors) - chunk_by_id.keys()
        if unknown:
            raise ValueError(f"eval row {index}: unknown distractors {sorted(unknown)}")
        if row.get("author_blind") is True:
            overlap = content_tokens(query) & content_tokens(chunk_by_id[relevant].text)
            if overlap:
                raise ValueError(
                    f"eval row {index}: author-blind content-token overlap: "
                    f"{sorted(overlap)}"
                )
            if not isinstance(row.get("source_query_id"), str):
                raise ValueError(
                    f"eval row {index}: author-blind row needs source_query_id"
                )


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return a two-sided 95% Wilson score interval for a binomial proportion."""
    if total <= 0:
        raise ValueError("Wilson interval requires a positive sample size")
    proportion = successes / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    center = (proportion + z2 / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total + z2 / (4.0 * total * total)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _apply_template(text: str, template: str | None) -> str:
    if template is None:
        return text
    return template.replace("{text}", text) if "{text}" in template else f"{template}{text}"


class EmbeddingClient:
    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        timeout_seconds: float,
        batch_size: int,
        query_template: str | None,
        doc_template: str | None,
    ) -> None:
        self.url = f"{endpoint.rstrip('/')}/v1/embeddings"
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.batch_size = batch_size
        self.query_template = query_template
        self.doc_template = doc_template

    async def _embed(
        self,
        session: aiohttp.ClientSession,
        texts: list[str],
        template: str | None,
    ) -> list[list[float]]:
        if not texts:
            return []
        request_texts = [
            _apply_template(text, template) if text.strip() else text for text in texts
        ]
        payload = {"model": self.model, "input": request_texts}
        async with session.post(self.url, json=payload) as response:
            response.raise_for_status()
            body = await response.json(content_type=None)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError(
                f"unexpected embedding response: expected {len(texts)} vectors, "
                f"got {len(data) if isinstance(data, list) else type(data).__name__}"
            )
        vectors: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                raise RuntimeError(f"unexpected embedding response item: {item!r}")
            vector = item.get("embedding")
            if not isinstance(vector, list):
                raise RuntimeError(f"unexpected embedding vector: {item!r}")
            vectors[item["index"]] = vector
        if set(vectors) != set(range(len(texts))):
            raise RuntimeError("embedding response indexes are incomplete or duplicated")
        return [vectors[index] for index in range(len(texts))]

    async def embed_docs(self, texts: list[str]) -> list[list[float]]:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                vectors.extend(await self._embed(session, batch, self.doc_template))
            return vectors

    async def embed_query(self, text: str) -> list[float]:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            return (await self._embed(session, [text], self.query_template))[0]


def _cosine_scores(query_vector: np.ndarray, document_matrix: np.ndarray) -> np.ndarray:
    query_norm = np.linalg.norm(query_vector)
    if query_norm == 0:
        raise ValueError("query embedding is zero/degenerate")
    return document_matrix @ (query_vector / query_norm)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    corpus = build_corpus()
    eval_rows = load_eval_set(args.eval_set)
    validate_eval_set(eval_rows, corpus)
    id_to_index = {chunk.chunk_id: index for index, chunk in enumerate(corpus)}
    client = EmbeddingClient(
        args.endpoint,
        args.model,
        timeout_seconds=args.timeout_seconds,
        batch_size=args.batch_size,
        query_template=args.query_template,
        doc_template=args.doc_template,
    )

    doc_start = time.perf_counter()
    document_vectors = await client.embed_docs([chunk.text for chunk in corpus])
    doc_embed_ms = (time.perf_counter() - doc_start) * 1000.0
    matrix = np.asarray(document_vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != len(corpus):
        raise RuntimeError(f"document matrix shape mismatch: {matrix.shape}")
    dimension = matrix.shape[1]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise RuntimeError("document embedding contains a zero/degenerate vector")
    matrix = matrix / norms

    if args.warmup:
        await client.embed_query(args.warmup)

    query_latencies_ms: list[float] = []
    hits_at_10 = 0
    distractor_wins = 0
    row_results: list[dict[str, Any]] = []
    for row in eval_rows:
        start = time.perf_counter()
        query_vector = np.asarray(
            await client.embed_query(row["query"]), dtype=np.float32
        )
        scores = _cosine_scores(query_vector, matrix)
        ranked_indices = np.argsort(-scores, kind="stable")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        query_latencies_ms.append(elapsed_ms)

        relevant_index = id_to_index[row["relevant_chunk_id"]]
        target_rank = int(np.flatnonzero(ranked_indices == relevant_index)[0]) + 1
        hit = target_rank <= 10
        hits_at_10 += int(hit)
        top_10 = [corpus[index].chunk_id for index in ranked_indices[:10]]
        target_score = float(scores[relevant_index])
        distractor_winner_ids = [
            distractor_id
            for distractor_id in row["distractor_chunk_ids"]
            if float(scores[id_to_index[distractor_id]]) > target_score
        ]
        distractor_win = bool(distractor_winner_ids)
        distractor_wins += int(distractor_win)
        row_results.append(
            {
                "distractor_win": distractor_win,
                "distractor_winner_ids": distractor_winner_ids,
                "embed_plus_search_ms": round(elapsed_ms, 3),
                "eval_id": row["id"],
                "eval_set": args.eval_set.stem,
                "hit_at_10": hit,
                "model": args.model,
                "pair_id": row.get("source_query_id", row["id"]),
                "query": row["query"],
                "rank": target_rank,
                "relevant_chunk_id": row["relevant_chunk_id"],
                "top_10": top_10,
            }
        )

    _write_jsonl(args.results_jsonl, row_results)
    total = len(eval_rows)
    recall_ci = wilson_interval(hits_at_10, total)
    distractor_ci = wilson_interval(distractor_wins, total)
    p95 = float(np.percentile(np.asarray(query_latencies_ms), 95))
    return {
        "status": "ok",
        "endpoint": args.endpoint,
        "model": args.model,
        "eval_set": str(args.eval_set),
        "results_jsonl": str(args.results_jsonl),
        "query_template": args.query_template,
        "doc_template": args.doc_template,
        "eval_rows": total,
        "corpus_chunks": len(corpus),
        "embedding_dimension": dimension,
        "document_embed_ms": round(doc_embed_ms, 3),
        "hits_at_10": hits_at_10,
        "recall_at_10": hits_at_10 / total,
        "recall_at_10_wilson_95": [round(value, 6) for value in recall_ci],
        "distractor_wins": distractor_wins,
        "distractor_win_rate": distractor_wins / total,
        "distractor_win_wilson_95": [round(value, 6) for value in distractor_ci],
        "p95_embed_plus_search_ms": round(p95, 3),
        "query_latency_ms": {
            "min": round(min(query_latencies_ms), 3),
            "median": round(float(np.percentile(query_latencies_ms, 50)), 3),
            "p95": round(p95, 3),
            "max": round(max(query_latencies_ms), 3),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:1234")
    parser.add_argument("--model", required=True)
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--results-jsonl", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--query-template")
    parser.add_argument("--doc-template")
    parser.add_argument(
        "--warmup",
        default="retrieval evaluation warmup",
        help="Text for one untimed query warmup; pass an empty string to disable.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - CLI reports stalled/unloadable models.
        result = {
            "status": "error",
            "endpoint": args.endpoint,
            "model": args.model,
            "eval_set": str(args.eval_set),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(json.dumps(result, indent=2))
        return 2
    encoded = json.dumps(result, indent=2)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
