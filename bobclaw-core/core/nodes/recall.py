from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.graph import AgentState
    from core.memory import FactStore
    from core.memory.models import Fact
    from core.memory.retriever import MemoryRetriever

log = logging.getLogger(__name__)


async def recall_node(
    state: AgentState,
    retriever: MemoryRetriever,
    fact_store: FactStore,
    *,
    enabled: bool,
    top_k: int = 5,
) -> dict:
    if not enabled:
        return {"recalled_facts": []}

    query_text = _last_user_message(state)
    if not query_text:
        return {"recalled_facts": []}

    chunks = await retriever.search(query_text, top_k=top_k)

    facts: list[Fact] = []
    for chunk in chunks:
        if chunk.source_fact_id:
            fact = await fact_store.get(chunk.source_fact_id)
            facts.append(fact)

    return {"recalled_facts": facts}


def _last_user_message(state: AgentState) -> str | None:
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, dict):
            if msg.get("role") == "user":
                return msg.get("content", "") or ""
        elif hasattr(msg, "role") and msg.role == "user":
            return getattr(msg, "content", "") or ""
    return None
