"""Bounded lane-local lexical recall when the packaged embedder is unavailable.

T0 reads exactly the assistant_response surface used by project_verbatim, never
other event fields. Unknown provider filters fail closed rather than broadening
the read. No vectors, model calls, or writes.
"""
import json
import re

from core.memory._db import connection
from core.memory.models import Hit
from core.memory.writer.tasks import chunk_fixed


async def search(db_path, query, *, top_k, filters=None, t0=False):
    filters = dict(filters or {})
    deprecated = filters.pop('include_deprecated', False)
    if filters or top_k < 1:
        return []
    tokens = list(dict.fromkeys(re.findall(r'\w+', query.casefold())))[:12]
    if not tokens:
        return []
    hits = []
    async with connection(db_path) as db:
        # Bound candidate materialization. SQL parameters keep query text inert.
        condition = ' OR '.join('instr(lower(body_json), ?) > 0' for _ in tokens)
        cursor = await db.execute(
            'SELECT fact_id, body_json FROM memory_facts WHERE (' + condition + ')'
            + ('' if deprecated else " AND json_extract(confidence_json, '$.rank') != 'deprecated'")
            + ' ORDER BY ts DESC LIMIT 200', tokens)
        for fact_id, body in await cursor.fetchall():
            content = json.dumps(json.loads(body), ensure_ascii=False)
            score = sum(token in content.casefold() for token in tokens) / len(tokens)
            hits.append(Hit(id=fact_id, score=score, payload=dict(text=content, source_fact_id=fact_id)))
        if t0:
            field = "json_extract(body_json, '$.assistant_response')"
            condition = ' OR '.join(f'instr(lower({field}), ?) > 0' for _ in tokens)
            cursor = await db.execute(
                'SELECT event_id, ts, ' + field + " FROM memory_events WHERE kind = 'agent_turn' AND ("
                + condition + ') ORDER BY insertion_order DESC LIMIT 200', tokens)
            for event_id, ts, response in await cursor.fetchall():
                if not isinstance(response, str):
                    continue
                for chunk in chunk_fixed([dict(event_id=event_id, ts=ts, assistant_response=response[:32768])]):
                    score = sum(token in chunk['text'].casefold() for token in tokens) / len(tokens)
                    if score:
                        hits.append(Hit(id=chunk['chunk_id'], score=score, payload=dict(
                            text=chunk['text'], projection_task='project_verbatim',
                            source_event_id=event_id, source_path=f'event://{event_id}')))
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:min(top_k, 200)]
