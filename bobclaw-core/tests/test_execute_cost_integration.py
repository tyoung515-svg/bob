"""BoBClaw Core — cost/cap INTEGRATION tests through ``execute_node`` (PRIOR-2/3).

Closes the two prior-audit test gaps (FABLE_AUDIT_KNOWN_ISSUES §8) at the level the
existing ``test_execute_warn_logging.py`` tests do NOT reach:

* **PRIOR-3** — the ``kimi_platform`` success branch's ``track_cost`` side-effect. The
  existing ``test_execute_tracks_cost_on_kimi_platform_success`` *mocks* ``track_cost``
  and only asserts the args it was called with. Here we let the REAL ``track_cost`` run
  and assert the recorded daily spend (``_cost._DAILY_USD``) matches ``usd_for(...)`` of
  the mocked usage — a real integration assertion on the cost side-effect, and the
  COST-1 tie (tracked cost is consistent with the mocked response's usage), not merely
  "a mock was called".

* **PRIOR-2** — the daily-cap abort branch. The existing
  ``test_execute_blocks_on_kimi_cap_reached`` mocks ``check_cap`` to force the block.
  Here we drive the block through the REAL ``check_cap`` cap-math by pre-loading the
  in-memory daily counter above the configured limit, exercising the real
  cap-math → block → abort → cap-string chain end-to-end and proving the live network
  seam (``KimiPlatformClient.chat``) is never touched.

Network-free: the only backend seam (``KimiPlatformClient.chat``) is mocked; the
autouse ``mock_redis`` fixture (tests/conftest.py) neutralises the escalation-pin
Redis read; the flight-spend meter fails open. pytest is socket-disabled
(``pytest.ini``: ``--disable-socket``), so any real egress would hard-error the test.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.backends import _cost as cost
from core.backends._cost import usd_for
from core.config import config


@pytest.fixture(autouse=True)
def _isolate_daily_usd():
    """Reset the in-memory daily-spend counter around each test (leak-proof)."""
    cost._DAILY_USD.clear()
    yield
    cost._DAILY_USD.clear()


# ─── PRIOR-3: track_cost REAL side-effect through the execute success branch ────

@pytest.mark.asyncio
async def test_execute_kimi_platform_success_records_real_spend():
    """Drive execute_node down the kimi_platform SUCCESS branch with a mocked
    network seam and the REAL track_cost, then assert the recorded daily spend
    equals usd_for(parsed usage). Pins the actual cost side-effect + the COST-1
    consistency (tracked USD == the rate-table cost of the mocked response's
    usage), which the mock-track_cost unit test never verifies."""
    from core.nodes.execute import execute_node

    raw = {
        "choices": [{"message": {"content": "hello from kimi"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cached_tokens": 20,
        },
    }
    # parse_usage: input = prompt - cached = 80; cached = 20; output = 50.
    expected_usd = usd_for(input_tokens=80, cached_tokens=20, output_tokens=50)

    with patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC:
        mock_instance = MockKPC.return_value
        mock_instance.chat = AsyncMock(return_value=raw)

        result = await execute_node({
            "task": "hi",
            "backend": "kimi_platform",
            "messages": [],
        })

    # The success branch actually ran (not the abort branch): the network seam
    # was hit exactly once and the mocked reply reached the caller.
    mock_instance.chat.assert_awaited_once()
    assert result["messages"][-1]["content"] == "hello from kimi"
    assert result["error"] is None

    # REAL side-effect: spend was recorded, and it is exactly the rate-table cost
    # of the mocked usage (COST-1 tie — the tracked number is consistent with the
    # provider-shaped usage on the response, not an unrelated guess).
    today = cost._today()
    assert today in cost._DAILY_USD, "track_cost did not record any spend"
    assert cost._DAILY_USD[today] == pytest.approx(expected_usd, rel=1e-9)
    assert expected_usd > 0.0  # guards against a silently-zero usage regression


@pytest.mark.asyncio
async def test_execute_kimi_platform_success_accumulates_across_calls():
    """Two successful metered turns accumulate spend (the real counter adds, it
    does not overwrite) — a second integration assertion on the durable side-effect."""
    from core.nodes.execute import execute_node

    raw = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 20},
    }
    per_call = usd_for(input_tokens=80, cached_tokens=20, output_tokens=50)

    with patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC:
        MockKPC.return_value.chat = AsyncMock(return_value=raw)
        await execute_node({"task": "a", "backend": "kimi_platform", "messages": []})
        await execute_node({"task": "b", "backend": "kimi_platform", "messages": []})

    assert cost._DAILY_USD[cost._today()] == pytest.approx(2 * per_call, rel=1e-9)


# ─── PRIOR-2: daily-cap abort via the REAL check_cap cap-math ────────────────────

@pytest.mark.asyncio
async def test_execute_kimi_platform_blocks_via_real_check_cap():
    """With today's in-memory spend pushed above the configured limit, the REAL
    check_cap returns block and execute_node ABORTS the backend, returning the
    cap-string WITHOUT ever calling KimiPlatformClient.chat. Drives the real
    cap-math → block → abort → cap-string chain (no check_cap mock)."""
    from core.nodes.execute import execute_node

    limit = config.KIMI_PLATFORM_DAILY_USD_LIMIT
    over = limit + 5.00
    cost._DAILY_USD[cost._today()] = over  # real check_cap will report "block"

    with patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC:
        result = await execute_node({
            "task": "hi",
            "backend": "kimi_platform",
            "messages": [],
        })

    # The live network seam was never constructed/called — the abort short-circuits
    # before KimiPlatformClient() is instantiated.
    MockKPC.assert_not_called()

    content = result["messages"][-1]["content"]
    assert "daily cap reached" in content
    assert f"${over:.2f}" in content       # the real accumulated total
    assert f"${limit:.2f}" in content      # the real configured limit
    assert result["error"] is None

    # And the counter was NOT advanced by the blocked turn (no track_cost ran).
    assert cost._DAILY_USD[cost._today()] == pytest.approx(over, rel=1e-9)


@pytest.mark.asyncio
async def test_execute_kimi_platform_success_when_under_cap_via_real_check_cap():
    """Counterpart to the block test: with spend at zero (under warn), the REAL
    check_cap returns ok, execute_node proceeds, and the metered call runs. Proves
    the abort test above blocks *because of* the cap-math, not unconditionally."""
    from core.nodes.execute import execute_node

    raw = {
        "choices": [{"message": {"content": "under-cap ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    with patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC:
        MockKPC.return_value.chat = AsyncMock(return_value=raw)
        result = await execute_node({
            "task": "hi",
            "backend": "kimi_platform",
            "messages": [],
        })

    MockKPC.return_value.chat.assert_awaited_once()
    assert result["messages"][-1]["content"] == "under-cap ok"
    assert result["error"] is None
