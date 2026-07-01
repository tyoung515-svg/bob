from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory._hashing import compute_input_hash
from core.memory.extractor import (
    FactExtractor,
    _EXTRACTOR_VERSION,
    _GENERATION_METHOD,
    _PROMPT_VERSION,
)
from core.memory.models import ConfidenceStub, Event, Fact, SlotResolution


class _MockFactStore:
    def __init__(self, existing_facts: list[Fact] | None = None) -> None:
        self.facts = existing_facts or []

    async def query(self, filters: dict) -> list[Fact]:
        if filters.get("generation_method") == _GENERATION_METHOD:
            return self.facts
        return []


def _mock_resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.get.return_value = SlotResolution(
        slot_name="extract_small",
        model="gemma-4-e4b-it",
        backend="lmstudio",
        endpoint="http://localhost:1234",
    )
    return resolver


def _agent_event(**overrides: str) -> Event:
    fields = {
        "event_id": "evt_001",
        "kind": "agent_turn",
        "ts": "2026-05-18T12:00:00+00:00",
        "hash": "abc123",
        "prev_hash": None,
        "body": {
            "user_message": "Hello",
            "assistant_response": "Hi there!",
        },
    }
    fields.update(overrides)
    return Event(**fields)


# ── test_extract_returns_empty_for_non_agent_turn ─────────────────────────────


@pytest.mark.asyncio
async def test_extract_returns_empty_for_non_agent_turn():
    event = _agent_event(kind="other")
    extractor = FactExtractor(_mock_resolver(), _MockFactStore())
    result = await extractor.extract(event)
    assert result == []


# ── test_extract_calls_resolved_backend ───────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_calls_resolved_backend():
    mock_fact_store = _MockFactStore()
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"facts": [{"text": "test fact"}]}'
                        }
                    }
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), mock_fact_store)
        result = await extractor.extract(_agent_event())

    instance.chat.assert_called_once()
    assert len(result) == 1
    assert result[0].body["text"] == "test fact"


# ── test_extract_parses_valid_json_facts ──────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_parses_valid_json_facts():
    llm_output = {
        "facts": [
            {"text": "fact one", "subject": "S1", "predicate": "P1"},
            {"text": "fact two"},
            {"text": "fact three", "subject": "S3"},
        ]
    }
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {"message": {"content": json.dumps(llm_output)}}
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert len(result) == 3
    assert result[0].body["text"] == "fact one"
    assert result[0].body["subject"] == "S1"
    assert result[0].body["predicate"] == "P1"
    assert result[1].body["text"] == "fact two"
    assert result[2].body["text"] == "fact three"
    assert result[2].body["subject"] == "S3"
    for f in result:
        assert isinstance(f, Fact)


# ── test_extract_handles_malformed_json_returns_empty ─────────────────────────


@pytest.mark.asyncio
async def test_extract_handles_malformed_json_returns_empty():
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {"message": {"content": "not valid json at all"}}
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert result == []


# ── test_extract_tolerates_chat_template_wrapping ─────────────────────────────


@pytest.mark.asyncio
async def test_extract_tolerates_markdown_fenced_json():
    """llama-server chat templates often fence the JSON in ```json ... ```;
    the parser must slice the object out instead of failing (live finding,
    2026-06-10 gemma-4-E4B on :8082)."""
    llm_output = {"facts": [{"text": "workshop is in Chiang Mai"}]}
    fenced = "```json\n" + json.dumps(llm_output) + "\n```"
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={"choices": [{"message": {"content": fenced}}]}
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert len(result) == 1
    assert result[0].body["text"] == "workshop is in Chiang Mai"


@pytest.mark.asyncio
async def test_extract_tolerates_preamble_before_json():
    llm_output = {"facts": [{"text": "test bench amp is the SALT-8"}]}
    wrapped = "Here are the extracted facts:\n" + json.dumps(llm_output)
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={"choices": [{"message": {"content": wrapped}}]}
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert len(result) == 1
    assert result[0].body["text"] == "test bench amp is the SALT-8"


# ── test_extract_handles_wrong_schema_returns_empty ───────────────────────────


@pytest.mark.asyncio
async def test_extract_handles_wrong_schema_returns_empty():
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"items": [{"text": "test"}]}'
                        }
                    }
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert result == []


# ── test_extract_event_level_dedup_via_existing_fact ──────────────────────────


@pytest.mark.asyncio
async def test_extract_event_level_dedup_via_existing_fact():
    event = _agent_event()
    inputs = {
        "event.body": event.body,
        "event.kind": event.kind,
        "extractor.version": _EXTRACTOR_VERSION,
        "prompt.version": _PROMPT_VERSION,
    }
    input_hash = compute_input_hash(_GENERATION_METHOD, inputs)
    existing_fact = Fact(
        fact_id="existing_001",
        generation_method=_GENERATION_METHOD,
        body={"text": "any"},
        source_event_id="old_event",
        input_hash=input_hash,
        confidence=ConfidenceStub(),
        ts="2026-05-18T00:00:00+00:00",
    )
    mock_fact_store = _MockFactStore([existing_fact])

    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"facts": [{"text": "new fact"}]}'
                        }
                    }
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), mock_fact_store)
        result = await extractor.extract(event)

    assert len(result) == 0


# ── test_extract_uses_extract_facts_from_event_generation_method ──────────────


@pytest.mark.asyncio
async def test_extract_uses_extract_facts_from_event_generation_method():
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"facts": [{"text": "v1 fact"}]}'
                        }
                    }
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert len(result) == 1
    assert result[0].generation_method == _GENERATION_METHOD


# ── test_extract_input_hash_varies_with_prompt_version ────────────────────────


@pytest.mark.asyncio
async def test_extract_input_hash_varies_with_prompt_version(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.memory.extractor._PROMPT_VERSION", "v2")
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"facts": [{"text": "test"}]}'
                        }
                    }
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert len(result) == 1
    assert result[0].generation_method == _GENERATION_METHOD
    assert result[0].input_hash != compute_input_hash(
        _GENERATION_METHOD,
        {
            "event.body": _agent_event().body,
            "event.kind": _agent_event().kind,
            "extractor.version": _EXTRACTOR_VERSION,
            "prompt.version": "v1",
        },
    )


# ── test_extract_sets_source_event_id_and_generation_method ───────────────────


@pytest.mark.asyncio
async def test_extract_sets_source_event_id_and_generation_method():
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"facts": [{"text": "a fact"}]}'
                        }
                    }
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event(event_id="evt_042"))

    assert len(result) == 1
    assert result[0].source_event_id == "evt_042"
    assert result[0].generation_method == _GENERATION_METHOD


# ── test_extract_facts_have_well_formed_confidence ────────────────────────────


@pytest.mark.asyncio
async def test_extract_facts_have_well_formed_confidence():
    llm_output = {
        "facts": [
            {"text": "fact A"},
            {"text": "fact B"},
        ]
    }
    with patch("core.backends.lmstudio.LMStudioClient") as mock_cls:
        instance = mock_cls.return_value
        instance.chat = AsyncMock(
            return_value={
                "choices": [
                    {"message": {"content": json.dumps(llm_output)}}
                ]
            }
        )
        extractor = FactExtractor(_mock_resolver(), _MockFactStore())
        result = await extractor.extract(_agent_event())

    assert len(result) == 2
    for f in result:
        assert isinstance(f.confidence, ConfidenceStub)
        assert f.confidence.rank == "normal"
        assert f.confidence.alpha == 1.0
        assert f.confidence.beta == 1.0
        assert f.confidence.decay_class == "stable_biographical"
