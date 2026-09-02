"""Test-pipe — deterministic chunking + resume + aggregate (SPEC §6, §9 unit 8).

The item set is split into ``N`` (2–4) DETERMINISTIC chunks (``id``-hash mod N — a
re-run assigns every item to the same chunk), each scored independently to a
per-chunk JSONL ledger. **Resume** = skip items already recorded in the ledger, so a
run killed mid-chunk resumes without re-scoring, and the final :func:`aggregate` is
IDENTICAL whether the run completed in one pass or was killed + resumed (SPEC §10
acceptance: "kill mid-run → resume → identical aggregate").

Network-free: :func:`run_chunk` takes an injected ``score_one`` callable, so the
resume/aggregate logic is proven with a synthetic scorer (no live runs).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core.testpipe.types import ItemResult, TestItem


def assign_chunk(item_id: str, n_chunks: int) -> int:
    """Deterministic chunk index for *item_id* (SPEC §6: id-hash, stable re-runs).

    SHA-256 of the id mod N — independent of item ORDER and of insertion, so two
    processes splitting the same set agree on assignment without coordination."""
    if n_chunks < 1:
        raise ValueError("n_chunks must be >= 1")
    h = int(hashlib.sha256(item_id.encode("utf-8")).hexdigest(), 16)
    return h % n_chunks


def split_chunks(items: list[TestItem], n_chunks: int) -> list[list[TestItem]]:
    """Partition *items* into ``n_chunks`` deterministic buckets (SPEC §6, 2–4).

    Within a chunk, items keep their input order (stable). Empty chunks are allowed
    (a small set may not populate all N)."""
    if not (2 <= n_chunks <= 4):
        # SPEC §6 bounds chunks to 2–4; enforce so a config typo fails loud.
        raise ValueError(f"n_chunks must be in 2..4 (SPEC §6), got {n_chunks}")
    buckets: list[list[TestItem]] = [[] for _ in range(n_chunks)]
    for it in items:
        buckets[assign_chunk(it.id, n_chunks)].append(it)
    return buckets


@dataclass
class ChunkLedger:
    """A per-chunk append-only JSONL results file with resume support (SPEC §6).

    Each line is one :class:`ItemResult`. :meth:`scored_ids` reads the file so a
    resumed run can skip already-scored items; :meth:`record` appends + flushes so a
    kill loses at most the in-flight item."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _parse_line(line: str) -> "Optional[ItemResult]":
        """Parse one ledger line → :class:`ItemResult`, or ``None`` on ANY failure.

        The SINGLE parse both :meth:`scored_ids` and :meth:`results` derive from, so the
        two can NEVER disagree (audit r1): a line that ``results`` would drop is also
        skipped by ``scored_ids`` (so the item is re-scored on resume, never silently
        counted-as-done-but-dropped-from-aggregate). Broad ``except`` covers a torn
        write (JSONDecodeError) AND a valid-JSON line missing a required field
        (TypeError/KeyError), keeping the readers provably consistent."""
        line = (line or "").strip()
        if not line:
            return None
        try:
            return ItemResult.from_dict(json.loads(line))
        except Exception:  # noqa: BLE001 — any parse failure ⇒ skip in BOTH readers
            return None

    def _parsed(self) -> list[ItemResult]:
        if not self.path.exists():
            return []
        return [
            r for r in (self._parse_line(ln)
                        for ln in self.path.read_text(encoding="utf-8").splitlines())
            if r is not None
        ]

    def scored_ids(self) -> set[str]:
        return {r.item_id for r in self._parsed()}

    def results(self) -> list[ItemResult]:
        return self._parsed()

    def record(self, result: ItemResult) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
            fh.flush()


ScoreOne = Callable[[TestItem], Awaitable[ItemResult]]


async def run_chunk(
    items: list[TestItem],
    ledger: ChunkLedger,
    score_one: ScoreOne,
    *,
    limit: Optional[int] = None,
) -> list[ItemResult]:
    """Score *items*, skipping any already in *ledger* (RESUME, SPEC §6).

    ``limit`` (test-only) stops after scoring that many NEW items — used to simulate
    a kill mid-chunk: run with ``limit=k``, then re-run with no limit and the resume
    skip means only the remaining items are scored, yet the aggregate is identical.
    Returns the results scored THIS call (already-scored items are not re-emitted)."""
    done = ledger.scored_ids()
    scored: list[ItemResult] = []
    for it in items:
        if it.id in done:
            continue
        if limit is not None and len(scored) >= limit:
            break
        result = await score_one(it)
        ledger.record(result)
        scored.append(result)
    return scored


@dataclass
class Aggregate:
    """The merged view over all chunk ledgers (SPEC §6 final ledger)."""

    n_items: int = 0
    n_hidden_pass: int = 0
    n_visible_pass: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    by_item: dict = field(default_factory=dict)   # item_id -> ItemResult

    @property
    def pass_at_1(self) -> float:
        return (self.n_hidden_pass / self.n_items) if self.n_items else 0.0

    def fingerprint(self) -> tuple:
        """An order-independent identity for 'identical aggregate' assertions —
        the sorted (id, hidden_pass, visible_pass, rounds) tuples."""
        return tuple(sorted(
            (r.item_id, r.hidden_pass, r.visible_pass, r.rounds)
            for r in self.by_item.values()
        ))


def aggregate(ledgers: list[ChunkLedger]) -> Aggregate:
    """Merge every chunk ledger into one :class:`Aggregate` (SPEC §6).

    De-duplicates by ``item_id`` (a resumed chunk may hold an item once; a
    belt-and-braces re-record keeps the LAST) so the aggregate is stable regardless
    of how the run was split or interrupted."""
    by_item: dict[str, ItemResult] = {}
    for lg in ledgers:
        for r in lg.results():
            by_item[r.item_id] = r
    agg = Aggregate(by_item=by_item)
    agg.n_items = len(by_item)
    agg.n_hidden_pass = sum(1 for r in by_item.values() if r.hidden_pass)
    agg.n_visible_pass = sum(1 for r in by_item.values() if r.visible_pass)
    agg.cost_usd = round(sum(r.cost_usd for r in by_item.values()), 6)
    agg.tokens = sum(r.tokens for r in by_item.values())
    return agg
