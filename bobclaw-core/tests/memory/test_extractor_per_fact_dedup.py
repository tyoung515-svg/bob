from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.memory.extractor import FactExtractor, _normalize
from core.memory.models import ConfidenceStub, Event, Fact


def _make_event(event_id: str = "evt_001", body: dict | None = None) -> Event:
    if body is None:
        body = {"user_message": "hello", "assistant_response": "hi there"}
    return Event(
        event_id=event_id,
        kind="agent_turn",
        body=body,
        ts="2026-05-18T12:00:00+00:00",
        hash="abc",
        prev_hash=None,
    )


class _StubSlotResolver:
    class _Resolution:
        backend = "lmstudio"
        endpoint = "http://localhost:1234"
        model = "gemma-4-e4b-it"

    def get(self, name: str) -> _Resolution:
        return self._Resolution()


class _StubFactStore:
    def __init__(self, existing_facts: list[Fact] | None = None):
        self.existing_facts = existing_facts or []
        self.put_calls: list[Fact] = []

    async def query(self, filters: dict) -> list[Fact]:
        return self.existing_facts

    async def put(self, fact: Fact) -> None:
        self.put_calls.append(fact)


@pytest.fixture
def empty_extractor() -> FactExtractor:
    return FactExtractor(
        slot_resolver=_StubSlotResolver(),
        fact_store=_StubFactStore(),
    )


@pytest.fixture
def extractor_with_facts() -> FactExtractor:
    existing = [
        Fact(
            fact_id="f_existing",
            generation_method="extract_facts_from_event",
            body={"text": "user likes cats"},
            source_event_id="evt_000",
            input_hash="some_hash",
            confidence=ConfidenceStub(),
            ts="2026-05-18T12:00:00+00:00",
        )
    ]
    return FactExtractor(
        slot_resolver=_StubSlotResolver(),
        fact_store=_StubFactStore(existing_facts=existing),
    )


class TestNormalize:
    def test_lowercases_text(self):
        assert _normalize("HELLO WORLD") == "hello world"

    def test_collapses_whitespace(self):
        assert _normalize("hello   world") == "hello world"

    def test_strips_trailing_punctuation(self):
        assert _normalize("hello world!") == "hello world"

    def test_strips_leading_punctuation(self):
        assert _normalize("!hello") == "hello"

    def test_full_pipeline(self):
        assert (
            _normalize("  Hello, World!!  ")
            == "hello, world"
        )

    def test_punctuation_middle_preserved(self):
        assert _normalize("hello, world") == "hello, world"


class TestPerFactDedup:
    @pytest.mark.asyncio
    async def test_suppresses_identical_text(
        self, extractor_with_facts: FactExtractor,
    ):
        event = _make_event("evt_001", {"user_message": "different", "assistant_response": "body"})
        facts = await extractor_with_facts._dedup_and_build_facts(
            [{"text": "user likes cats"}], event,
        )
        assert len(facts) == 0

    @pytest.mark.asyncio
    async def test_preserves_distinct_facts(
        self, extractor_with_facts: FactExtractor,
    ):
        event = _make_event("evt_002")
        facts = await extractor_with_facts._dedup_and_build_facts(
            [{"text": "user likes dogs"}], event,
        )
        assert len(facts) == 1
        assert facts[0].body["text"] == "user likes dogs"

    @pytest.mark.asyncio
    async def test_event_level_still_works(
        self, empty_extractor: FactExtractor,
    ):
        event = _make_event("evt_001")
        facts1 = await empty_extractor._dedup_and_build_facts(
            [{"text": "some fact"}], event,
        )
        assert len(facts1) == 1

        fact = facts1[0]
        extractor_with_same = FactExtractor(
            slot_resolver=_StubSlotResolver(),
            fact_store=_StubFactStore(existing_facts=[fact]),
        )
        facts2 = await extractor_with_same._dedup_and_build_facts(
            [{"text": "some fact"}], event,
        )
        assert len(facts2) == 0

    @pytest.mark.asyncio
    async def test_punctuation_variants(
        self, extractor_with_facts: FactExtractor,
    ):
        event = _make_event("evt_003")
        facts = await extractor_with_facts._dedup_and_build_facts(
            [{"text": "user likes cats!"}], event,
        )
        assert len(facts) == 0

    @pytest.mark.asyncio
    async def test_empty_text_does_not_error(
        self, extractor_with_facts: FactExtractor,
    ):
        event = _make_event("evt_004")
        facts = await extractor_with_facts._dedup_and_build_facts(
            [{"text": ""}, {"text": "user likes dogs"}], event,
        )
        assert len(facts) == 2
        texts = [f.body["text"] for f in facts]
        assert "" in texts
        assert "user likes dogs" in texts

    @pytest.mark.asyncio
    async def test_mixed_preserve_and_suppress(
        self, extractor_with_facts: FactExtractor,
    ):
        event = _make_event("evt_005")
        facts = await extractor_with_facts._dedup_and_build_facts(
            [
                {"text": "user likes cats", "subject": "user", "predicate": "likes cats"},
                {"text": "user likes dogs"},
                {"text": "user likes birds"},
            ],
            event,
        )
        assert len(facts) == 2
        texts = {f.body["text"] for f in facts}
        assert texts == {"user likes dogs", "user likes birds"}

    @pytest.mark.asyncio
    async def test_whitespace_variants(
        self, extractor_with_facts: FactExtractor,
    ):
        event = _make_event("evt_006")
        facts = await extractor_with_facts._dedup_and_build_facts(
            [{"text": "  user   likes   cats  "}], event,
        )
        assert len(facts) == 0
