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


async def _run_t0_projection(singletons, event) -> None:
    """Project one freshly appended event without delaying the chat response."""
    try:
        result = await singletons.writer.project(event)
        logger.info(
            "t0_projected event_id=%s status=%s chunks=%d",
            event.event_id, result.status, result.chunk_count,
        )
        singletons.last_writer_error = None
    except Exception as exc:
        singletons.last_writer_error = exc
        logger.exception("t0_projection_failed event_id=%s", event.event_id)


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
        logger.exception(
            "Failed to get MemorySingletons for L0 event append; continuing turn"
        )
        return

    event_log = singletons.event_log

    user_message = ""
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            user_message = msg.get("content", "") or ""
            break
        if getattr(msg, "role", None) == "user":
            user_message = getattr(msg, "content", "") or ""
            break
    # The live API state keeps the current turn in `task`; `messages` contains
    # only prior-history system text until execute_node runs.  Falling back here
    # fixes the 268/269 empty-user capture defect while preserving explicit user
    # messages used by direct graph/tests callers.
    if not user_message:
        task = state.get("task")
        if isinstance(task, str):
            user_message = task

    body: dict[str, Any] = {
        "user_message": user_message,
        "assistant_response": assistant_response,
        "face_id": state.get("face_id"),
        "turn_id": state.get("turn_id") or uuid.uuid4().hex,
        "cost_usd": state.get("cost_usd"),
        "duration_ms": state.get("duration_ms"),
        "model_capability_class": state.get("model_capability_class"),
        "error": error_msg,
        # Conversation + owner, so a turn can be cited, expanded and access-checked later.
        "conversation_id": state.get("conversation_id"),
        "user_id": state.get("user_id"),
        "history": state.get("history_fit"),
    }

    try:
        event = await event_log.atomic_append(body)
    except Exception as exc:
        singletons.last_l0_append_error = exc
        logger.exception(
            "L0 agent-turn append failed for turn_id=%s; continuing turn",
            body["turn_id"],
        )
        return

    singletons.last_l0_append_error = None

    if config.MEMORY_WRITER_ENABLED and getattr(singletons, "writer", None) is not None:
        task = asyncio.create_task(
            _run_t0_projection(singletons, event),
            name=f"t0_projection:{event.event_id}",
        )
        singletons.pending_writer_tasks.add(task)
        task.add_done_callback(singletons.pending_writer_tasks.discard)

    if config.MEMORY_L1_EXTRACTION_ENABLED:
        task = asyncio.create_task(
            _run_l1_extraction(singletons, event),
            name=f"l1_extraction:{event.event_id}",
        )
        singletons.pending_extraction_tasks.add(task)
        task.add_done_callback(singletons.pending_extraction_tasks.discard)

    # Claims compaction (ON by default; BOB_COMPACTOR_ENABLED=false stops it): when the history has grown large and
    # the conversation runs on a cloud backend, extend its chain of verified claims in the background. The prompt
    # assembler uses the chain only once the history no longer fits. Never blocks or fails the turn.
    try:
        from core.context.compactor import maybe_schedule_compaction

        maybe_schedule_compaction(state)
    except Exception:
        logger.debug("context: compaction scheduling failed", exc_info=True)
