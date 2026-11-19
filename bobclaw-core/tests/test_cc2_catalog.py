"""Sanitized Claude catalog observations and signed-out uncertainty fencing."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_cc1_catalog import lane, configure
from core import lane_config as lc


@pytest.mark.asyncio
async def test_unknown_auth_status_fences_next_turn_and_catalog(lane, monkeypatch):
    configure(lane, BOB_LANE_BACKENDS='claude_code', CLAUDE_SIGNED_IN='true', CLAUDE_CODE_MODEL='sonnet')
    monkeypatch.setattr(lc, 'claude_unavailable_reason', lambda lane: 'Sign in to Claude again')
    rows = await lc.catalog(SimpleNamespace(discover=AsyncMock(return_value=[])))
    assert rows[0]['available'] is False and rows[0]['unavailable_reason'] == 'Sign in to Claude again'
    with pytest.raises(lc.LaneConfigurationError, match='Sign in to Claude again'):
        lc.require_backend('claude_code', 'sonnet')


@pytest.mark.asyncio
async def test_reported_model_and_subscription_metadata_are_shown(lane, monkeypatch):
    configure(lane, BOB_LANE_BACKENDS='claude_code', CLAUDE_SIGNED_IN='true', CLAUDE_CODE_MODEL='sonnet')
    monkeypatch.setattr(lc, 'claude_observations', lambda lane: {'models': {'sonnet': 'claude-sonnet-full'}})
    rows = await lc.catalog(SimpleNamespace(discover=AsyncMock(return_value=[])))
    row = rows[0]
    assert row['provider_reported_model'] == 'claude-sonnet-full'
    assert row['billing'] == 'subscription' and row['tool_capable']
    spec = next(iter(lc.agent_service_definitions(rows)['models'].values()))
    assert spec['family'] == 'anthropic' and spec['model_identity'] == 'anthropic/claude'
    assert 'reported_model_names' not in spec
