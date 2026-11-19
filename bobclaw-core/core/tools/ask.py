"""
BoBClaw Core — mid-turn question to the operator.

S2 (2026-10-27). Hermes asks the operator a question *inside* the turn and keeps
working with the answer; the Bob portal face could only end its turn and hope.
This tool closes that gap: it publishes a ``question`` frame on the turn's custom
stream, parks the tool call on an asyncio future, and returns the operator's
answer to the model as the tool result.

Transport shape:

    assistant tool_calls -> ask_operator(question, choices?)
    core emits  {"type": "question", "question_id", "question", "choices"}
    portal POSTs the answer -> gateway -> core /api/chat/questions/{id}/answer
    core resolves the future -> the tool returns the answer -> the loop continues

Questions and accepted answers are persisted for identified conversation turns.
A turn that is cancelled or times out returns an explicit "no answer" string so
the model can proceed honestly instead of hanging.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

MAX_CHOICES = 4
MAX_QUESTION_CHARS = 199
DEFAULT_TIMEOUT_S = 600.0

# The turn's stream writer, bound by the tool loop around the invocation.
_writer: contextvars.ContextVar = contextvars.ContextVar("ask_operator_writer", default=None)

# question_id -> future awaiting the operator's answer (the running event loop's).
_PENDING: dict[str, "asyncio.Future[str]"] = {}


def bind_writer(writer):
    """Bind the turn's stream writer to the current context; returns a token."""
    return _writer.set(writer)


def unbind_writer(token) -> None:
    try:
        _writer.reset(token)
    except (ValueError, RuntimeError):
        pass


def pending_questions() -> list[str]:
    return [qid for qid, fut in _PENDING.items() if not fut.done()]


def answer_question(question_id: str, answer: str) -> bool:
    """Resolve one parked question. Returns False when it is unknown or already done."""
    # Pop first: two answers arriving together must not both claim the question
    # (the future is not "done" until the scheduled callback runs, so a check on
    # done() alone would let the second caller through).
    future = _PENDING.pop(question_id, None)
    if future is None or future.done():
        return False
    try:
        future.get_loop().call_soon_threadsafe(_resolve, future, answer)
    except RuntimeError:  # loop already closed
        return False
    return True


def _resolve(future: "asyncio.Future[str]", answer: str) -> None:
    if not future.done():
        future.set_result(answer)


def _clean_choices(choices) -> list[str]:
    if not isinstance(choices, (list, tuple)):
        return []
    out = []
    for choice in choices:
        text = str(choice).strip()
        if text:
            out.append(text)
    return out[:MAX_CHOICES]


@tool
async def ask_operator(question: str, choices: Optional[list[str]] = None) -> str:
    """Ask the operator a question and wait for the answer before continuing.

    Use this instead of ending a turn with a question in prose when the next step
    genuinely depends on the operator's decision. Use one short sentence under
    200 characters; put any plan or brief in answer text before calling this tool. Pass
    ``choices`` when there are a small number of concrete answers — the portal
    renders them as buttons. The answer comes back to you as this tool's result;
    the operator may also type something you did not offer.
    """
    return await _ask_question(question, choices)


async def _ask_question(question, choices=None, *, origin=None):
    """Shared transport; origin is platform-only and absent from the tool schema."""
    text = str(question or "").strip()
    if not text:
        return json.dumps({"ok": False, "error": "question is required"})
    if len(text) > MAX_QUESTION_CHARS:
        return json.dumps({'ok': False, 'error': 'question must be under 200 characters; put the plan in answer text first'})

    writer = _writer.get()
    if writer is None:
        return json.dumps(
            {"ok": False, "error": "no question channel on this turn (not a streamed turn)"}
        )

    question_id = uuid.uuid4().hex
    offered = _clean_choices(choices)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _PENDING[question_id] = future
    from core.execution.workflow_context import current as workflow_current
    workflow = workflow_current.get()
    timeout = workflow.wait_seconds if workflow else DEFAULT_TIMEOUT_S
    workflow_wait = None
    try:
        from core.tools.projects import _current_user_id, _current_conversation_id
        from core.execution.chat_records import question as save_question, request_id
        import time
        try:
            args = (question_id, _current_user_id.get(), _current_conversation_id.get(), text, offered, time.time()+timeout)
            saved = await save_question(*args, origin=origin) if origin else await save_question(*args)
            if origin and not saved:
                return json.dumps({'ok': False, 'error': 'a stored question is required'})
            if workflow:
                workflow_wait = await workflow.open_wait('question',question_id,text,offered)
        except Exception:
            logger.exception('Could not persist operator question')
            return json.dumps({'ok':False,'error':'question_storage_unavailable; no question was sent'})
        writer(
            {
                "type": "question",
                "question_id": question_id,
                "question": text,
                "choices": offered,
                **({"origin": origin} if origin else {}),
                **({"request_id": request_id.get()} if origin else {}),
            }
        )
        try:
            answer = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "ok": False,
                    "timed_out": True,
                    "error": "the operator did not answer in time; continue without it and say so",
                }
            )
        return json.dumps({"ok": True, "answer": answer,
                           **({"question_id": question_id} if origin else {})}, ensure_ascii=False)
    finally:
        _PENDING.pop(question_id, None)
        if workflow_wait and future.done() and not future.cancelled():
            await workflow.close_wait(workflow_wait, {'answer':future.result()})


def can_request_step_extension() -> bool:
    from core.execution import chat_records
    from core.tools.projects import _current_user_id, _current_conversation_id
    return (_writer.get() is not None and bool(_current_user_id.get()) and bool(_current_conversation_id.get())
            and bool(chat_records.request_id.get()) and chat_records.POOL is not None)


async def request_step_extension(steps: int) -> bool:
    """Only a committed owner answer to our own platform question grants steps."""
    from core.execution import chat_records
    from core.tools.projects import _current_user_id, _current_conversation_id
    if not can_request_step_extension():
        return False
    try:
        raw = await _ask_question(
            f"I have used {steps} steps on this reply and I am not finished. Would you like me to keep going?",
            ["Keep going", "Stop here"], origin="step_limit")
        result = json.loads(raw)
        if not result.get('ok') or result.get('answer') != 'Keep going':
            return False
        return bool(await chat_records.POOL.fetchval(
            '''SELECT 1 FROM chat_questions q JOIN conversations c
                 ON c.id=q.conversation_id AND c.user_id=q.user_id
               WHERE q.id=$1 AND q.user_id=$2 AND q.conversation_id=$3
                 AND q.request_id=$4 AND q.origin='step_limit'
                 AND q.answer='Keep going' AND q.answered_at IS NOT NULL''',
            result['question_id'], _current_user_id.get(),
            uuid.UUID(_current_conversation_id.get()), chat_records.request_id.get(), timeout=5))
    except asyncio.CancelledError:
        return False
    except Exception:
        logger.exception('Could not verify a stored step extension answer')
        return False


def reset_pending_questions() -> None:
    """Test hook: drop every parked question (their turns must not hang a suite)."""
    for future in list(_PENDING.values()):
        if not future.done():
            try:
                future.get_loop().call_soon_threadsafe(_resolve, future, "")
            except RuntimeError:
                pass
    _PENDING.clear()
