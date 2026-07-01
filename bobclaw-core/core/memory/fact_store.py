from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from core.memory._db import connection
from core.memory.exceptions import HashingError, L1ValidationFailed
from core.memory.models import ConfidenceStub, Fact

_INPUT_HASH_PATTERN = re.compile(r"^blake3:[0-9a-f]{64}$")

_VALID_FILTER_KEYS: frozenset[str] = frozenset({
    "generation_method",
    "source_event_id",
    "rank",
})


class SQLiteFactStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def put(self, fact: Fact) -> str:
        if not _INPUT_HASH_PATTERN.match(fact.input_hash):
            raise HashingError(
                f"malformed input_hash for fact {fact.fact_id!r}: "
                f"expected 'blake3:' + 64 hex chars, got {fact.input_hash!r}"
            )
        body = fact.body
        schema_version = body.get("_schema_version")
        if schema_version is not None:
            from core.memory.schema_evolution import (
                CURRENT_SCHEMA_VERSION,
                upgrade_body_to_latest,
            )

            if schema_version != CURRENT_SCHEMA_VERSION:
                new_body = upgrade_body_to_latest(
                    fact.generation_method, body
                )
                fact = Fact(
                    fact_id=fact.fact_id,
                    generation_method=fact.generation_method,
                    body=new_body,
                    source_event_id=fact.source_event_id,
                    input_hash=fact.input_hash,
                    confidence=fact.confidence,
                    ts=fact.ts,
                )
        async with connection(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO memory_facts "
                "(fact_id, generation_method, body_json, source_event_id, input_hash, confidence_json, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    fact.fact_id,
                    fact.generation_method,
                    json.dumps(fact.body, sort_keys=True),
                    fact.source_event_id,
                    fact.input_hash,
                    json.dumps(asdict(fact.confidence), sort_keys=True),
                    fact.ts,
                ),
            )
            await db.commit()
        return fact.fact_id

    async def get(self, fact_id: str) -> Fact:
        async with connection(self._db_path) as db:
            cursor = await db.execute(
                "SELECT fact_id, generation_method, body_json, source_event_id, "
                "input_hash, confidence_json, ts "
                "FROM memory_facts WHERE fact_id = ?",
                (fact_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise L1ValidationFailed(
                fact_id, ["fact not found"]
            )
        return self._row_to_fact(row)

    async def query(self, filters: dict) -> list[Fact]:
        unknown = set(filters.keys()) - _VALID_FILTER_KEYS
        if unknown:
            raise L1ValidationFailed(
                "query",
                [f"unknown filter keys: {sorted(unknown)}"],
            )
        clauses: list[str] = []
        params: list[str] = []
        if "generation_method" in filters:
            clauses.append("generation_method = ?")
            params.append(filters["generation_method"])
        if "source_event_id" in filters:
            clauses.append("source_event_id = ?")
            params.append(filters["source_event_id"])
        if "rank" in filters:
            clauses.append("json_extract(confidence_json, '$.rank') = ?")
            params.append(filters["rank"])
        where = ""
        if clauses:
            where = " WHERE " + " AND ".join(clauses)
        async with connection(self._db_path) as db:
            cursor = await db.execute(
                "SELECT fact_id, generation_method, body_json, source_event_id, "
                "input_hash, confidence_json, ts "
                "FROM memory_facts" + where,
                params,
            )
            rows = await cursor.fetchall()
        return [self._row_to_fact(r) for r in rows]

    async def delete(self, fact_id: str) -> None:
        async with connection(self._db_path) as db:
            await db.execute(
                "DELETE FROM memory_facts WHERE fact_id = ?",
                (fact_id,),
            )
            await db.commit()

    async def all_ids(self) -> list[str]:
        async with connection(self._db_path) as db:
            cursor = await db.execute(
                "SELECT fact_id FROM memory_facts ORDER BY fact_id"
            )
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _row_to_fact(row) -> Fact:
        return Fact(
            fact_id=row[0],
            generation_method=row[1],
            body=json.loads(row[2]),
            source_event_id=row[3],
            input_hash=row[4],
            confidence=ConfidenceStub(**json.loads(row[5])),
            ts=row[6],
        )
