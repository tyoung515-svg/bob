from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from core.config import config

if TYPE_CHECKING:
    from core.graph import AgentState

logger = logging.getLogger(__name__)


async def _run_l1_extraction(singletons, event) -> None:
    try:
        new_facts = await singletons.extractor.extract(event)
        for fact in new_facts:
            await singletons.fact_store.put(fact)
        if new_facts:
            await singletons.indexer.reindex_facts(
                [f.fact_id for f in new_facts]
            )
        logger.info(
            "l1_extracted event_id=%s count=%d",
            event.event_id, len(new_facts),
        )
    except Exception as exc:
        singletons.last_extraction_error = exc
        logger.exception(
            "l1_extraction_failed event_id=%s", event.event_id
        )


async def _append_agent_turn_event(
    state: "AgentState",
    *,
    assistant_response: str,
    error_msg: str | None = None,
) -> None:
    if not config.MEMORY_ENABLED:
        return

    from core.memory.bootstrap import get_memory

    try:
        singletons = get_memory()
    except Exception:
        logger.exception("Failed to get MemorySingletons for L0 event append")
        raise

    event_log = singletons.event_log

    user_message = ""
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_message = msg.get("content", "")
            break

    body: dict[str, Any] = {
        "user_message": user_message,
        "assistant_response": assistant_response,
        "face_id": state.get("face_id"),
        "turn_id": state.get("turn_id") or uuid.uuid4().hex,
        "cost_usd": state.get("cost_usd"),
        "duration_ms": state.get("duration_ms"),
        "model_capability_class": state.get("model_capability_class"),
        "error": error_msg,
    }

    event = await event_log.atomic_append(body)

    if config.MEMORY_L1_EXTRACTION_ENABLED:
        task = asyncio.create_task(
            _run_l1_extraction(singletons, event),
            name=f"l1_extraction:{event.event_id}",
        )
        singletons.pending_extraction_tasks.add(task)
        task.add_done_callback(singletons.pending_extraction_tasks.discard)
