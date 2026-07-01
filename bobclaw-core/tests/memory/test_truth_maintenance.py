from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.exceptions import TruthMaintenanceError
from core.memory.models import (
    ConfidenceStub,
    Event,
    Fact,
)
from core.memory.truth_maintenance import (
    TruthMaintenancePipeline,
)


def _source_weights_path(tmp_path: Path) -> Path:
    p = tmp_path / "source_weights.toml"
    p.write_text(
        '[meta]\nspec_version = "1.0"\n\n[weights]\n'
        'user_assertion = 1.0\n'
        'tool_output = 0.5\n'
        'llm_inference = 0.2\n',
        encoding="utf-8",
    )
    return p


@pytest.fixture
def fact() -> Fact:
    return Fact(
        fact_id="fct_1",
        generation_method="extract_facts_from_event",
        body={"text": "test"},
        source_event_id="evt_orig",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(
            alpha=4.0,
            beta=1.0,
            rank="normal",
            decay_class="stable_biographical",
        ),
        ts="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def event() -> Event:
    return Event(
        event_id="evt_corroborate",
        kind="user_assertion",
        body={"text": "corroborating evidence"},
        ts="2026-05-13T00:00:00+00:00",
        hash="blake3:" + "b" * 64,
        prev_hash=None,
    )


@pytest.fixture
def mock_fact_store(fact: Fact):
    store = MagicMock()
    store.get = AsyncMock(return_value=fact)
    store.put = AsyncMock(side_effect=lambda f: f.fact_id)
    return store


@pytest.fixture
def pipeline(mock_fact_store, tmp_path: Path) -> TruthMaintenancePipeline:
    return TruthMaintenancePipeline(
        fact_store=mock_fact_store,
        source_weights_path=_source_weights_path(tmp_path),
    )


@pytest.mark.asyncio
async def test_corroborate_user_assertion_adds_alpha_1(pipeline, event, mock_fact_store):
    result = await pipeline.corroborate("fct_1", event, "user_assertion")
    assert result.confidence.alpha == 4.0 + 1.0
    assert result.confidence.beta == 1.0


@pytest.mark.asyncio
async def test_corroborate_tool_output_adds_alpha_half(pipeline, event, mock_fact_store):
    result = await pipeline.corroborate("fct_1", event, "tool_output")
    assert result.confidence.alpha == 4.0 + 0.5


@pytest.mark.asyncio
async def test_corroborate_llm_inference_adds_alpha_02(pipeline, event, mock_fact_store):
    result = await pipeline.corroborate("fct_1", event, "llm_inference")
    assert result.confidence.alpha == 4.0 + 0.2


@pytest.mark.asyncio
async def test_corroborate_sets_corroboration_fields(pipeline, event, mock_fact_store):
    result = await pipeline.corroborate("fct_1", event, "user_assertion")
    assert result.confidence.last_corroboration_event_id == "evt_corroborate"
    assert result.confidence.last_corroboration_ts == "2026-05-13T00:00:00+00:00"


@pytest.mark.asyncio
async def test_unknown_source_kind_raises(pipeline, event):
    with pytest.raises(TruthMaintenanceError):
        await pipeline.corroborate("fct_1", event, "unknown_source")


@pytest.mark.asyncio
async def test_contradict_adds_beta_symmetrically(pipeline, event, mock_fact_store):
    result = await pipeline.contradict("fct_1", event, "tool_output")
    assert result.confidence.beta == 1.0 + 0.5
    assert result.confidence.alpha == 4.0


@pytest.mark.asyncio
async def test_contradict_sets_corroboration_fields(pipeline, event, mock_fact_store):
    result = await pipeline.contradict("fct_1", event, "tool_output")
    assert result.confidence.last_corroboration_event_id == "evt_corroborate"
    assert result.confidence.last_corroboration_ts == "2026-05-13T00:00:00+00:00"


@pytest.mark.asyncio
async def test_deprecate_sets_rank_deprecated_preserves_alpha_beta(pipeline, mock_fact_store):
    result = await pipeline.deprecate("fct_1", "superseded")
    assert result.confidence.rank == "deprecated"
    assert result.confidence.alpha == 4.0
    assert result.confidence.beta == 1.0


@pytest.mark.asyncio
async def test_multiple_corroborations_stack_additively(pipeline, event, mock_fact_store):
    r1 = await pipeline.corroborate("fct_1", event, "user_assertion")
    assert r1.confidence.alpha == 5.0

    mock_fact_store.get.return_value = r1
    r2 = await pipeline.corroborate("fct_1", event, "user_assertion")
    assert r2.confidence.alpha == 6.0

    mock_fact_store.get.return_value = r2
    r3 = await pipeline.corroborate("fct_1", event, "tool_output")
    assert r3.confidence.alpha == 6.5


@pytest.mark.asyncio
async def test_put_called_with_updated_fact(pipeline, event, mock_fact_store):
    await pipeline.corroborate("fct_1", event, "user_assertion")
    mock_fact_store.put.assert_called_once()
    written = mock_fact_store.put.call_args[0][0]
    assert written.confidence.alpha == 5.0
    assert written.confidence.last_corroboration_event_id == "evt_corroborate"
