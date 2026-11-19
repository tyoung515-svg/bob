"""BoBClaw Core — face-callable memory tools (F2 fix, 2026-10-01).

Native tools for explicit notes and recall:

- ``memory_save``   — durable note write: hash-chained L0 event (kind
  ``agent_turn`` so the existing verbatim writer embeds it), an awaited T0
  projection (so the confirmation means the note is actually searchable), and
  a structured ``memory_facts`` row keyed to the event. Provenance
  (face / user / conversation) rides module-level contextvars that
  ``execute.py`` sets around the tool-calling loop.
- ``memory_recall`` — semantic search over the same T0 chunk store the
  automatic "Prior context" splice reads.

- ``memory_retire`` and ``memory_correct`` preserve the owner's saved-note
  evidence and record retirement through the truth-maintenance writer.

Tools fail legibly (in-band ``[error:...]`` strings) rather than raising into
the tool loop, mirroring the MCP action-tool contract.
"""
from __future__ import annotations

import logging
import json
import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.tools import tool

log = logging.getLogger(__name__)

# ── Provenance context (set by execute.py around the tool loop) ──────────────
memory_tool_face_id: ContextVar[str] = ContextVar("memory_tool_face_id", default="")
memory_tool_user_id: ContextVar[str | None] = ContextVar("memory_tool_user_id", default=None)
memory_tool_conversation_id: ContextVar[str] = ContextVar(
    "memory_tool_conversation_id", default=""
)


def _singletons():
    # Late import: bootstrap is heavy and these tools must stay importable in
    # test contexts where memory is never bootstrapped.
    from core.memory.bootstrap import get_memory

    return get_memory()


async def _save_note(text: str) -> tuple[str, str | None]:
    """Save a durable note to Bob's long-term memory.

    Use when the operator asks you to remember something (a fact, preference,
    decision, project note). The note is hash-chained into the memory event
    log, embedded for semantic recall, and stored as a structured fact.
    Returns the event/fact ids as verifiable proof of the write.
    """
    text = (text or "").strip()
    if not text:
        return "[error:empty] nothing to save", None
    try:
        singletons = _singletons()
    except Exception:
        return "[error:memory_unavailable] memory subsystem not bootstrapped", None

    from core.memory._hashing import blake3_hex
    from core.memory.models import ConfidenceStub, Fact

    now = datetime.now(timezone.utc)
    face = memory_tool_face_id.get() or "unknown"
    fact_id = uuid.uuid4().hex
    # RETRIEVAL CONTRACT: the verbatim T0 writer (writer/tasks.py) chunks and
    # embeds ONLY the event's assistant_response field — user_message is L0
    # audit evidence but never reaches Qdrant. The note text must therefore
    # ride in BOTH fields: assistant_response so the note is actually
    # recallable, user_message for the provenance-prefixed audit trail.
    body = {
        "user_message": f"[memory_save:{face}] {text}",
        "assistant_response": f"[memory_save:{face} fact_id={fact_id}] stored note: {text}",
        "face_id": face,
        "turn_id": uuid.uuid4().hex,
        "cost_usd": None,
        "duration_ms": None,
        "model_capability_class": None,
        "error": None,
        "source": "memory_save_tool",
        "user_id": memory_tool_user_id.get(),
        "conversation_id": memory_tool_conversation_id.get() or None,
    }
    try:
        event = await singletons.event_log.atomic_append(body)
    except Exception as exc:
        log.exception("memory_save: L0 append failed")
        return f"[error:l0_append] {type(exc).__name__}: {exc}", None

    # Await the T0 projection (chunk + embed + index) so "Saved" means the
    # note is searchable NOW, not eventually. The write fence is enforced
    # inside the provider seam this calls through.
    indexed = False
    if getattr(singletons, "writer", None) is not None:
        try:
            result = await singletons.writer.project(event)
            index_status = f"{result.status} chunks={result.chunk_count}"
            indexed = result.status.lower() in {"completed", "already_completed"}
        except Exception as exc:
            log.exception("memory_save: T0 projection failed event=%s", event.event_id)
            index_status = f"index_failed:{type(exc).__name__}"
    else:
        index_status = "writer_disabled"

    try:
        await singletons.fact_store.put(
            Fact(
                fact_id=fact_id,
                generation_method="face_memory_save:v1",
                body={"text": text, "face_id": face},
                source_event_id=event.event_id,
                input_hash=blake3_hex(text.encode("utf-8")),
                confidence=ConfidenceStub(),
                ts=now.isoformat(),
            )
        )
        fact_status = "stored"
    except Exception as exc:
        log.exception("memory_save: fact put failed event=%s", event.event_id)
        fact_status = f"fact_failed:{type(exc).__name__}"

    receipt = (
        f"Saved to long-term memory. event_id={event.event_id} "
        f"fact_id={fact_id} index={index_status} fact={fact_status}"
    )
    return receipt, fact_id if indexed and fact_status == "stored" else None


@tool
async def memory_save(text: str) -> str:
    """Save a durable note with event/fact ids and awaited search indexing status.

    Save requested facts, preferences or decisions. Check both fact and index
    statuses before claiming full success.
    """
    receipt, _ = await _save_note(text)
    return receipt


async def _owned_note(singletons, fact_id):
    from core.memory.exceptions import L1ValidationFailed

    user = memory_tool_user_id.get()
    if not user or not memory_tool_conversation_id.get() or not memory_tool_face_id.get():
        return None, "[error:missing_identity] an agent, authenticated user and conversation are required"
    try:
        fact = await singletons.fact_store.get(fact_id)
    except L1ValidationFailed:
        return None, "[error:not_found] this saved note was not found for this user"
    if fact.generation_method != "face_memory_save:v1":
        return None, "[error:not_saved_note] only notes created by memory_save can be changed"
    # Older notes do not carry user_id in their fact body. The immutable source
    # event is the authority, never a model-supplied argument or display label.
    event = await singletons.event_log.get(fact.source_event_id)
    if event.body.get("source") != "memory_save_tool" or event.body.get("user_id") != user:
        return None, "[error:not_found] this saved note was not found for this user"
    return fact, None


async def _retire_note(singletons, fact, reason, replacement_fact_id=None):
    from pathlib import Path
    from core.memory.truth_maintenance import TruthMaintenancePipeline

    body = dict(source="memory_retire_tool", fact_id=fact.fact_id, reason=reason,
                replacement_fact_id=replacement_fact_id,
                face_id=memory_tool_face_id.get(), user_id=memory_tool_user_id.get(),
                conversation_id=memory_tool_conversation_id.get(),
                # Audit only: never embed a retired note or its reason as new history.
                assistant_response="")
    event = await singletons.event_log.atomic_append(body)
    retirement = {**body, "event_id": event.event_id, "ts": event.ts}
    pipeline = TruthMaintenancePipeline(singletons.fact_store,
        Path(__file__).resolve().parents[2] / "config/source_weights.toml")
    await pipeline.deprecate(fact.fact_id, reason, retirement=retirement)
    return (f"Retired saved note fact_id={fact.fact_id}. Reason: {reason} "
            f"event_id={event.event_id}. Nothing was deleted.")


@tool
async def memory_retire(fact_id: str, reason: str) -> str:
    """Retire one of this user's saved notes by fact id, with a one-sentence reason.

    Keeps the note and audit evidence, but hides it from recall. Extracted facts,
    imported memory and another user's notes cannot be changed.
    """
    reason = " ".join((reason or "").split())
    if not reason or len(reason) > 1000:
        return "[error:invalid_reason] give a one-sentence reason of at most 1000 characters"
    try:
        singletons = _singletons()
        fact, error = await _owned_note(singletons, fact_id)
        if error:
            return error
        if fact.confidence.rank == "deprecated":
            return f"Saved note fact_id={fact_id} is already retired."
        return await _retire_note(singletons, fact, reason)
    except Exception as exc:
        log.exception("memory_retire failed")
        return f"[error:retire] the note could not be retired ({type(exc).__name__})"


@tool
async def memory_correct(fact_id: str, text: str, reason: str) -> str:
    """Save corrected text as a new searchable note, then retire the old saved note.

    Takes the old fact id, corrected text and a one-sentence reason. If the new
    save or indexing fails, the old note remains active. Nothing is deleted.
    """
    reason = " ".join((reason or "").split())
    if not reason or len(reason) > 1000:
        return "[error:invalid_reason] give a one-sentence reason of at most 1000 characters"
    try:
        singletons = _singletons()
        fact, error = await _owned_note(singletons, fact_id)
        if error:
            return error
        if fact.confidence.rank == "deprecated":
            return f"Saved note fact_id={fact_id} is already retired."
        receipt, new_id = await _save_note(text)
        if new_id is None:
            return f"[error:correction_save] the old note was left active. New save result: {receipt}"
        try:
            retired = await _retire_note(singletons, fact,
                f"{reason.rstrip('.!?')}; replaced by saved note fact_id={new_id}.", new_id)
        except Exception as exc:
            log.exception("memory_correct: replacement saved but retirement failed")
            return (f"[error:correction_retire] the replacement was saved: {receipt}\n"
                    f"Retirement of fact_id={fact_id} was not confirmed ({type(exc).__name__}); inspect it before retrying.")
        return f"Corrected saved note fact_id={fact_id}. {receipt}\n{retired}"
    except Exception as exc:
        log.exception("memory_correct failed")
        return f"[error:correct] correction did not finish ({type(exc).__name__}); inspect the saved notes before retrying"


@tool
async def memory_recall(query: str) -> str:
    """Search Bob's long-term memory for saved notes and past conversations
    related to the query. Returns the top matching snippets, or a no-results
    note. Use before saying you don't know something that might be stored.
    """
    query = (query or "").strip()
    if not query:
        return "[error:empty] no query given"
    try:
        singletons = _singletons()
    except Exception:
        return "[error:memory_unavailable] memory subsystem not bootstrapped"
    from core.memory.exceptions import EmbedderUnavailable

    try:
        chunks = await singletons.retriever.search(query, top_k=5)
    except EmbedderUnavailable:
        # Use the existing lane-local fact store, never another lane/corpus or
        # an unscoped vector-client bypass. This does not cover T0-only history.
        log.warning("memory_recall: embedder unavailable; using stored-fact keywords")
        flag = {"degraded": "embedder_unavailable", "coverage": "stored_facts_only"}
        try:
            terms = set(re.findall(r"\w+", query.casefold()))
            matches = []
            for fact in await singletons.fact_store.query({}):
                if fact.confidence.rank == "deprecated":
                    continue
                content = json.dumps(fact.body, ensure_ascii=False, sort_keys=True)
                words = set(re.findall(r"\w+", content.casefold()))
                score = len(terms & words)
                if score:
                    kind = "saved note" if fact.generation_method == "face_memory_save:v1" else "extracted fact"
                    matches.append((score, fact.fact_id, content, kind))
            matches.sort(key=lambda item: (-item[0], item[1]))
            flag["hits"] = [{"fact_id": fid, "kind": kind, "content": content[:2000]}
                            for _, fid, content, kind in matches[:5]]
        except Exception as exc:
            log.warning("memory_recall: keyword search failed (%s)", type(exc).__name__)
            flag["error"] = "keyword_search_unavailable"
        return json.dumps(flag, ensure_ascii=False)
    except Exception as exc:
        log.exception("memory_recall: search failed")
        return f"[error:recall] {type(exc).__name__}: {exc}"
    if not chunks:
        return "No memory found for that query."
    lines = []
    for chunk in chunks[:5]:
        label = "conversation history"
        if chunk.source_fact_id:
            from core.memory.exceptions import L1ValidationFailed
            try:
                fact = await singletons.fact_store.get(chunk.source_fact_id)
            except L1ValidationFailed:
                continue
            if fact.confidence.rank == "deprecated":
                continue
            kind = "saved note" if fact.generation_method == "face_memory_save:v1" else "extracted fact"
            label = f"{kind} fact_id={fact.fact_id}"
        lines.append(f"- {chunk.content}\n  [{label}]")
    return "Memory hits:\n" + "\n".join(lines) if lines else "No memory found for that query."
