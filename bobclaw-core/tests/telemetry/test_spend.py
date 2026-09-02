"""Flight substrate L0.4 — per-flight spend meter.

Proves:
  * record + read aggregate per (flight, backend) and sum to ``usd``;
  * a spend record emits a ``cost`` monitor frame (state-change);
  * zero/negative deltas are no-ops; ambient bucket for a None flight;
  * the ``current_flight`` contextvar + ``record_current_flight_spend`` attribute to the
    bound flight;
  * Redis-down FAILS OPEN to the process-local store (never raises);
  * ``_cost.usd_for`` matches the rate math ``track_cost`` uses.

HERMETIC: an in-memory fake Redis is injected for the meter's store so the tests are
deterministic whether or not a real Redis (Docker) is up.
"""
from __future__ import annotations

import json

import pytest

from core.backends import _cost
from core.telemetry import emit as emit_mod
from core.telemetry import spend as spend_mod
from core.telemetry.spend import (
    current_flight,
    flight_spend,
    record_current_flight_spend,
    record_flight_spend,
    reset_current_flight,
    reset_flight_spend,
    set_current_flight,
)


class _FakeHashRedis:
    """Minimal in-memory Redis hash store (hincrbyfloat/hgetall/delete)."""

    def __init__(self):
        self.h: dict[str, dict[str, float]] = {}

    async def hincrbyfloat(self, key, field, amount):
        self.h.setdefault(key, {})
        self.h[key][field] = self.h[key].get(field, 0.0) + float(amount)
        return self.h[key][field]

    async def hgetall(self, key):
        return {k: str(v) for k, v in self.h.get(key, {}).items()}

    async def delete(self, key):
        self.h.pop(key, None)


class _FakePub:
    def __init__(self):
        self.published = []

    async def publish(self, channel, data):
        self.published.append((channel, data))


class _Boom:
    async def hincrbyfloat(self, *a, **k):
        raise RuntimeError("no redis")

    async def hgetall(self, *a, **k):
        raise RuntimeError("no redis")

    async def delete(self, *a, **k):
        raise RuntimeError("no redis")

    async def publish(self, *a, **k):
        raise RuntimeError("no redis")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Inject an in-memory store for the meter + a no-op stream writer, and clear the
    process-local fallback so each test starts clean."""
    store = _FakeHashRedis()
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: store)
    monkeypatch.setattr("core.nodes.execute._get_stream_writer", lambda: None)
    # Quiet emit's redis by default (tests that assert on the cost frame re-patch it).
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: _Boom())
    spend_mod._LOCAL_SPEND.clear()
    return store


# ── record + read ─────────────────────────────────────────────────────────────
async def test_record_and_read_aggregates():
    await record_flight_spend("ms-5", "kimi_platform", 0.10, emit=False)
    await record_flight_spend("ms-5", "kimi_platform", 0.05, emit=False)
    await record_flight_spend("ms-5", "deepseek_v4_flash", 0.02, emit=False)
    snap = await flight_spend("ms-5")
    assert snap["by_backend"]["kimi_platform"] == 0.15
    assert snap["by_backend"]["deepseek_v4_flash"] == 0.02
    assert snap["usd"] == 0.17


async def test_zero_and_negative_are_noops():
    await record_flight_spend("ms-z", "kimi_platform", 0.0, emit=False)
    await record_flight_spend("ms-z", "kimi_platform", -1.0, emit=False)
    snap = await flight_spend("ms-z")
    assert snap["usd"] == 0.0
    assert snap["by_backend"] == {}


async def test_none_flight_buckets_to_ambient():
    await record_flight_spend(None, "kimi_platform", 0.03, emit=False)
    assert (await flight_spend("ambient"))["usd"] == 0.03
    assert (await flight_spend(None))["usd"] == 0.03


async def test_reset_clears_a_flight():
    await record_flight_spend("ms-r", "kimi_platform", 0.05, emit=False)
    await reset_flight_spend("ms-r")
    assert (await flight_spend("ms-r"))["usd"] == 0.0


# ── cost-frame emission ───────────────────────────────────────────────────────
async def test_record_emits_cost_frame(monkeypatch):
    pub = _FakePub()
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: pub)

    snap = await record_flight_spend("ms-emit", "kimi_platform", 0.25)

    assert snap["usd"] == 0.25
    assert len(pub.published) == 1
    ch, data = pub.published[0]
    frame = json.loads(data)
    assert ch == "bobclaw:monitor"
    assert frame["type"] == "cost"
    assert frame["flight_id"] == "ms-emit"
    assert frame["delta_usd"] == 0.25
    assert frame["usd"] == 0.25
    assert frame["by_backend"]["kimi_platform"] == 0.25


# ── Redis-down fail-open ──────────────────────────────────────────────────────
async def test_failopen_to_local_when_redis_down(monkeypatch):
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: _Boom())
    # Must NOT raise; the delta lands in the process-local store and reads back.
    await record_flight_spend("ms-fo", "kimi_platform", 0.09, emit=False)
    assert (await flight_spend("ms-fo"))["usd"] == 0.09


# ── contextvar attribution ────────────────────────────────────────────────────
async def test_record_current_flight_spend_uses_contextvar():
    tok = set_current_flight("ms-ctx")
    try:
        assert current_flight() == "ms-ctx"
        await record_current_flight_spend("kimi_platform", 0.07)
    finally:
        reset_current_flight(tok)
    assert (await flight_spend("ms-ctx"))["usd"] == 0.07
    assert current_flight() is None


# ── usd_for parity ────────────────────────────────────────────────────────────
def test_usd_for_matches_track_cost_rates():
    assert abs(_cost.usd_for(input_tokens=1_000_000) - 0.95) < 1e-9
    assert abs(_cost.usd_for(output_tokens=1_000_000) - 4.00) < 1e-9
