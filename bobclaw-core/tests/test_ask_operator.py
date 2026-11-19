"""
BoBClaw Core — ask_operator (mid-turn question) unit tests.

No network, no model: the tool's only outside contact is the turn's stream writer
(here a list append) and the answer function the HTTP route calls.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import core.tools.ask as ask


@pytest.fixture(autouse=True)
def _clean_pending():
    ask.reset_pending_questions()
    yield
    ask.reset_pending_questions()


@pytest.mark.asyncio
async def test_question_is_published_and_the_answer_returns_to_the_model():
    frames: list[dict] = []
    token = ask.bind_writer(frames.append)
    try:
        task = asyncio.create_task(
            ask.ask_operator.ainvoke(
                {"question": "Which window should I sweep?", "choices": ["30 days", "45 days"]}
            )
        )
        # The tool parks until an answer arrives via the HTTP route's function.
        for _ in range(50):
            if frames:
                break
            await asyncio.sleep(0.01)
        assert frames, "the question frame was never published"
        frame = frames[0]
        assert frame["type"] == "question"
        assert frame["question"] == "Which window should I sweep?"
        assert frame["choices"] == ["30 days", "45 days"]

        assert ask.answer_question(frame["question_id"], "45 days") is True
        result = json.loads(await asyncio.wait_for(task, timeout=2))
    finally:
        ask.unbind_writer(token)

    assert result == {"ok": True, "answer": "45 days"}
    assert ask.pending_questions() == []


@pytest.mark.asyncio
async def test_a_second_answer_for_the_same_question_is_refused():
    frames: list[dict] = []
    token = ask.bind_writer(frames.append)
    try:
        task = asyncio.create_task(ask.ask_operator.ainvoke({"question": "Pick one"}))
        for _ in range(50):
            if frames:
                break
            await asyncio.sleep(0.01)
        qid = frames[0]["question_id"]
        assert ask.answer_question(qid, "first") is True
        assert ask.answer_question(qid, "second") is False
        await asyncio.wait_for(task, timeout=2)
    finally:
        ask.unbind_writer(token)


@pytest.mark.asyncio
async def test_timeout_returns_an_honest_no_answer(monkeypatch):
    monkeypatch.setattr(ask, "DEFAULT_TIMEOUT_S", 0.05)
    token = ask.bind_writer(lambda frame: None)
    try:
        raw = await ask.ask_operator.ainvoke({"question": "Still there?"})
    finally:
        ask.unbind_writer(token)
    result = json.loads(raw)
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert "did not answer" in result["error"]


@pytest.mark.asyncio
async def test_without_a_stream_writer_it_refuses_instead_of_hanging():
    raw = await ask.ask_operator.ainvoke({"question": "Anybody?"})
    result = json.loads(raw)
    assert result["ok"] is False
    assert "no question channel" in result["error"]


@pytest.mark.asyncio
async def test_empty_question_is_refused():
    raw = await ask.ask_operator.ainvoke({"question": "   "})
    assert json.loads(raw)["error"] == "question is required"
