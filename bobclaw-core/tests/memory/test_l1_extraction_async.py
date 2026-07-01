from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.memory._hashing import _compute_event_hash
from core.memory.models import ConfidenceStub, Event, Fact


class _StubExtractor:
    def __init__(self, delay: float = 0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.call_count = 0

    async def extract(self, event: Event) -> list:
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return [
            Fact(
                fact_id="f1",
                generation_method="extract_facts_from_event",
                body={"text": "stub fact"},
                source_event_id=event.event_id,
                input_hash="stub_hash_f1",
                confidence=ConfidenceStub(),
                ts="2026-05-18T12:00:00+00:00",
            )
        ]


class _StubFactStore:
    def __init__(self):
        self.put_calls: list[Any] = []

    async def put(self, fact: Any) -> None:
        self.put_calls.append(fact)

    async def query(self, filters: dict) -> list:
        return []


class _StubIndexer:
    def __init__(self):
        self.reindex_calls: list[list[str]] = []

    async def reindex_facts(self, fact_ids: list[str]) -> None:
        self.reindex_calls.append(fact_ids)


class _StubEventLog:
    def __init__(self, event_id: str = "evt_001"):
        self.event_id = event_id
        self.atomic_append_calls: list[dict] = []

    async def atomic_append(self, body: dict) -> Event:
        self.atomic_append_calls.append(body)
        prev_hash = None
        h = _compute_event_hash(body, prev_hash)
        return Event(
            event_id=self.event_id,
            kind="agent_turn",
            body=body,
            ts="2026-05-18T12:00:00+00:00",
            hash=h,
            prev_hash=prev_hash,
        )


@dataclass
class _StubSingletons:
    event_log: _StubEventLog
    extractor: _StubExtractor
    fact_store: _StubFactStore
    indexer: _StubIndexer
    pending_extraction_tasks: set[asyncio.Task] = field(default_factory=set)
    last_extraction_error: Exception | None = None

    async def drain_extraction_tasks(self) -> None:
        tasks = list(self.pending_extraction_tasks)
        if not tasks:
            return
        self.pending_extraction_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)


def _minimal_state(**overrides: Any) -> dict:
    state = {
        "messages": [{"role": "user", "content": "Hello"}],
        "face_id": "assistant",
        "turn_id": None,
        "cost_usd": None,
        "duration_ms": None,
        "model_capability_class": None,
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _reset_bootstrap(monkeypatch: pytest.MonkeyPatch):
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = None
    _b._bootstrap_config_snapshot = None


@pytest.fixture
def _enable_memory_l1(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "core.config.config.MEMORY_L1_EXTRACTION_ENABLED", True, raising=False
    )


@pytest.fixture
def stubs() -> _StubSingletons:
    return _StubSingletons(
        event_log=_StubEventLog(),
        extractor=_StubExtractor(),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )


async def _call_append(stubs: _StubSingletons, monkeypatch: pytest.MonkeyPatch):
    from core.nodes._l0_events import _append_agent_turn_event
    from core.memory.bootstrap import _bootstrap_singleton
    _bootstrap_singleton = stubs

    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Test response.",
    )


@pytest.mark.asyncio
async def test_agent_turn_returns_before_extraction_completes(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_latency"),
        extractor=_StubExtractor(delay=0.5),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    start = time.perf_counter()
    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Fast response.",
    )
    elapsed = time.perf_counter() - start

    assert elapsed < 0.1, (
        f"_append_agent_turn_event took {elapsed:.3f}s, "
        f"expected <0.1s despite 0.5s extraction"
    )

    await stubs.drain_extraction_tasks()


@pytest.mark.asyncio
async def test_extraction_task_is_registered_on_singletons(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_reg"),
        extractor=_StubExtractor(delay=0.05),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    assert len(stubs.pending_extraction_tasks) == 0

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Test.",
    )

    assert len(stubs.pending_extraction_tasks) == 1, (
        f"Expected 1 pending task, got {len(stubs.pending_extraction_tasks)}"
    )

    await stubs.drain_extraction_tasks()


@pytest.mark.asyncio
async def test_drain_extraction_tasks_awaits_completion(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_drain"),
        extractor=_StubExtractor(delay=0.05),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Test.",
    )

    assert stubs.extractor.call_count == 0

    await stubs.drain_extraction_tasks()

    assert stubs.extractor.call_count == 1
    assert len(stubs.pending_extraction_tasks) == 0


@pytest.mark.asyncio
async def test_extraction_failure_does_not_propagate(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_fail"),
        extractor=_StubExtractor(fail=True),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Still works.",
    )

    assert stubs.last_extraction_error is None

    await stubs.drain_extraction_tasks()

    assert isinstance(stubs.last_extraction_error, RuntimeError)
    assert str(stubs.last_extraction_error) == "boom"


@pytest.mark.asyncio
async def test_l0_event_durable_when_extraction_fails(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_durable"),
        extractor=_StubExtractor(fail=True),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Durable.",
    )

    assert len(stubs.event_log.atomic_append_calls) == 1

    await stubs.drain_extraction_tasks()

    body = stubs.event_log.atomic_append_calls[0]
    assert body["assistant_response"] == "Durable."
    prev_hash = None
    expected_hash = _compute_event_hash(body, prev_hash)
    assert expected_hash is not None


@pytest.mark.asyncio
async def test_extraction_disabled_skips_task(
    monkeypatch,
):
    monkeypatch.setattr("core.config.config.MEMORY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "core.config.config.MEMORY_L1_EXTRACTION_ENABLED", False, raising=False
    )

    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_dis"),
        extractor=_StubExtractor(),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Disabled.",
    )

    assert len(stubs.pending_extraction_tasks) == 0
    assert stubs.extractor.call_count == 0


@pytest.mark.asyncio
async def test_two_concurrent_turns_register_two_tasks(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_concurrent"),
        extractor=_StubExtractor(delay=0.05),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    async def turn(msg: str) -> None:
        await _append_agent_turn_event(
            _minimal_state(),
            assistant_response=msg,
        )

    await asyncio.gather(turn("A"), turn("B"))

    assert len(stubs.pending_extraction_tasks) == 2, (
        f"Expected 2 pending tasks, got {len(stubs.pending_extraction_tasks)}"
    )

    await stubs.drain_extraction_tasks()

    assert len(stubs.pending_extraction_tasks) == 0
    assert stubs.extractor.call_count == 2


@pytest.mark.asyncio
async def test_task_is_removed_from_set_on_completion(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_removed"),
        extractor=_StubExtractor(delay=0.02),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Removed.",
    )

    await stubs.drain_extraction_tasks()

    assert len(stubs.pending_extraction_tasks) == 0


@pytest.mark.asyncio
async def test_last_extraction_error_replaced_on_subsequent_failure(
    _enable_memory_l1, monkeypatch,
):
    first_fail = _StubExtractor(fail=True)
    second_fail = _StubExtractor(fail=True)
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_twofail"),
        extractor=first_fail,
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="First.",
    )
    await stubs.drain_extraction_tasks()
    first_error = stubs.last_extraction_error

    stubs.extractor = second_fail

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="Second.",
    )
    await stubs.drain_extraction_tasks()
    second_error = stubs.last_extraction_error

    assert first_error is not second_error, (
        "second failure should replace (not reuse) the error object"
    )


@pytest.mark.asyncio
async def test_successful_extraction_does_not_set_last_error(
    _enable_memory_l1, monkeypatch,
):
    stubs = _StubSingletons(
        event_log=_StubEventLog("evt_ok"),
        extractor=_StubExtractor(),
        fact_store=_StubFactStore(),
        indexer=_StubIndexer(),
    )
    import core.memory.bootstrap as _b
    _b._bootstrap_singleton = stubs
    _b._bootstrap_config_snapshot = object()

    from core.nodes._l0_events import _append_agent_turn_event

    await _append_agent_turn_event(
        _minimal_state(),
        assistant_response="OK.",
    )
    await stubs.drain_extraction_tasks()

    assert stubs.last_extraction_error is None
