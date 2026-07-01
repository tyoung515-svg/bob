"""
BoBClaw Core — Unit tests for Kimi Platform cost tracker & cap
"""
from __future__ import annotations

import pytest

from core.backends import _cost as cost


# ─── track_cost ───────────────────────────────────────────────────────────────

def test_track_cost_computes_correctly():
    cost._DAILY_USD.clear()
    total = cost.track_cost(
        input_tokens=1_000_000,
        cached_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # $0.95 + $0.16 + $4.00 = $5.11
    assert total == pytest.approx(5.11, rel=1e-6)


def test_track_cost_accumulates_same_day():
    cost._DAILY_USD.clear()
    cost.track_cost(input_tokens=1_000_000)
    total = cost.track_cost(input_tokens=1_000_000)
    # 2 × $0.95 = $1.90
    assert total == pytest.approx(1.90, rel=1e-6)


# ─── check_cap ────────────────────────────────────────────────────────────────

def test_check_cap_returns_ok_below_warn():
    cost._DAILY_USD.clear()
    cost._DAILY_USD[cost._today()] = 5.00
    ok, total, mode = cost.check_cap()
    assert ok is True
    assert total == pytest.approx(5.00, rel=1e-6)
    assert mode == "ok"


def test_check_cap_returns_warn_between_warn_and_cap():
    cost._DAILY_USD.clear()
    cost._DAILY_USD[cost._today()] = 12.00
    ok, total, mode = cost.check_cap()
    assert ok is True
    assert total == pytest.approx(12.00, rel=1e-6)
    assert mode == "warn"


def test_check_cap_returns_block_at_or_above_cap():
    cost._DAILY_USD.clear()
    cost._DAILY_USD[cost._today()] = 20.00
    ok, total, mode = cost.check_cap()
    assert ok is False
    assert total == pytest.approx(20.00, rel=1e-6)
    assert mode == "block"


# ─── Date rollover ────────────────────────────────────────────────────────────

def test_yesterdays_spend_does_not_count_today():
    cost._DAILY_USD.clear()
    cost._DAILY_USD["2099-01-01"] = 100.00
    ok, total, mode = cost.check_cap()
    assert ok is True
    assert total == pytest.approx(0.00, rel=1e-6)
    assert mode == "ok"


# ─── parse_usage ──────────────────────────────────────────────────────────────

def test_parse_usage_extracts_moonshot_shape():
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cached_tokens": 20,
        }
    }
    result = cost.parse_usage(raw)
    assert result["input_tokens"] == 80
    assert result["cached_tokens"] == 20
    assert result["output_tokens"] == 50


def test_parse_usage_defaults_to_zero_on_missing():
    result = cost.parse_usage({})
    assert result["input_tokens"] == 0
    assert result["cached_tokens"] == 0
    assert result["output_tokens"] == 0


def test_parse_usage_no_cache_hit_returns_full_input():
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
        }
    }
    result = cost.parse_usage(raw)
    assert result["input_tokens"] == 100
    assert result["cached_tokens"] == 0
    assert result["output_tokens"] == 50


def test_parse_usage_all_cached_yields_zero_input():
    raw = {
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 30,
            "cached_tokens": 50,
        }
    }
    result = cost.parse_usage(raw)
    assert result["input_tokens"] == 0
    assert result["cached_tokens"] == 50
    assert result["output_tokens"] == 30
