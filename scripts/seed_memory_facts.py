#!/usr/bin/env python3
"""
Seed L1 facts into the memory module, then index them via the Indexer.

Usage:
    python scripts/seed_memory_facts.py --store-id bobclaw_default --facts-json ./seed_facts.json

Idempotent: re-running with the same fact_ids is a no-op (INSERT OR REPLACE
with the same body does not change the stored row). To force an overwrite
with a different body, either change the fact_id or include the new body
in the facts JSON — the put() call uses INSERT OR REPLACE.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import blake3

from core.config import config as core_config
from core.memory.bootstrap import (
    MemoryBootstrapConfig,
    bootstrap_memory,
    get_memory,
)
from core.memory.fact_store import SQLiteFactStore
from core.memory.models import ConfidenceStub, Fact

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("seed_memory_facts")


def _compute_input_hash(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    h = blake3.blake3(raw).hexdigest()
    return f"blake3:{h}"


def _parse_facts(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("facts", [])
    return list(raw)


def ensure_parent_events(db_path, facts_data: list[dict[str, Any]]) -> None:
    """Insert a synthetic memory_events row for every source_event_id the facts
    reference. memory_facts.source_event_id is a FK to memory_events(event_id);
    seeded facts have no real L0 event, so we create idempotent sentinel events.
    insertion_order is set to -(i+1) (negative, unique per sentinel) so that
    all synthetic events sort strictly before any real event.
    """
    import asyncio

    import aiosqlite

    event_ids = {
        item.get("source_event_id", "seed-000") for item in facts_data
    }

    async def _do() -> None:
        async with aiosqlite.connect(str(db_path)) as db:
            for i, eid in enumerate(sorted(event_ids)):
                await db.execute(
                    "INSERT OR IGNORE INTO memory_events "
                    "(event_id, kind, body_json, ts, hash, prev_hash, insertion_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (eid, "seed", "{}", datetime.now(timezone.utc).isoformat(),
                     f"seed:{eid}", None, -(i + 1)),
                )
            await db.commit()
            log.info("Ensured %d parent seed event(s)", len(event_ids))

    asyncio.run(_do())


def seed_facts(
    fact_store: SQLiteFactStore,
    facts_data: list[dict[str, Any]],
) -> None:
    import asyncio

    async def _do_seed() -> list[str]:
        ids: list[str] = []
        for item in facts_data:
            fact_id = item["fact_id"]
            body = item.get("body", {})
            input_hash = item.get("input_hash")
            if not input_hash:
                input_hash = _compute_input_hash(body)
            ts = item.get("ts", datetime.now(timezone.utc).isoformat())
            fact = Fact(
                fact_id=fact_id,
                generation_method=item.get(
                    "generation_method", "seed_script"
                ),
                body=body,
                source_event_id=item.get(
                    "source_event_id", "seed-000"
                ),
                input_hash=input_hash,
                confidence=ConfidenceStub(
                    alpha=item.get("confidence", {}).get("alpha", 1.0),
                    beta=item.get("confidence", {}).get("beta", 1.0),
                    rank=item.get("confidence", {}).get("rank", "normal"),
                ),
                ts=ts,
            )
            await fact_store.put(fact)
            ids.append(fact_id)
            log.info("Seeded fact: %s", fact_id)
        return ids

    return asyncio.run(_do_seed())


def reindex_facts(fact_ids: list[str]) -> None:
    import asyncio

    async def _do_reindex() -> None:
        memory = get_memory()
        stats = await memory.indexer.reindex_facts(fact_ids)
        if stats.errors:
            log.warning("Reindex errors: %s", stats.errors)
        log.info(
            "Reindexed %d facts: %d chunks changed, %d errors",
            stats.facts_processed,
            stats.chunks_changed,
            len(stats.errors),
        )

    asyncio.run(_do_reindex())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed L1 facts into the memory module"
    )
    parser.add_argument(
        "--store-id",
        default="bobclaw_default",
        help="Store ID to seed into (default: bobclaw_default)",
    )
    parser.add_argument(
        "--facts-json",
        required=True,
        type=Path,
        help="Path to JSON file with facts array",
    )
    args = parser.parse_args()

    facts_data = _parse_facts(args.facts_json)
    if not facts_data:
        log.warning("No facts found in %s", args.facts_json)
        sys.exit(0)

    log.info("Loading %d facts from %s", len(facts_data), args.facts_json)
    bcfg = MemoryBootstrapConfig.from_env(core_config)
    bcfg = MemoryBootstrapConfig(
        enabled=True,
        sqlite_path=bcfg.sqlite_path,
        qdrant_url=bcfg.qdrant_url,
        stores_config_path=bcfg.stores_config_path,
        default_store_id=args.store_id,
    )

    bootstrap_memory(bcfg)

    fact_store = SQLiteFactStore(bcfg.sqlite_path)
    ensure_parent_events(bcfg.sqlite_path, facts_data)
    ids = seed_facts(fact_store, facts_data)
    log.info("Seeded %d facts into store %s", len(ids), args.store_id)

    reindex_facts(ids)
    log.info("Done")


if __name__ == "__main__":
    main()
