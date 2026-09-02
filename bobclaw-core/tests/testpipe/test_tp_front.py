"""Test-pipe — front adapters (SPEC §3, §9 unit 3)."""
from __future__ import annotations

from core.testpipe.fixtures import fixture_items
from core.testpipe.front import apply_front, front_context
from core.testpipe.types import FRONT_MODEL, FRONT_PASSTHROUGH, SlotConfig


def _add():
    return fixture_items()[0]


async def test_passthrough_makes_no_model_call():
    calls = []

    async def send(messages, backend, model=None):
        calls.append(backend)
        return "should not be called"

    cfg = SlotConfig(name="B0", front=FRONT_PASSTHROUGH)
    plan = await apply_front(_add(), cfg, send=send)
    assert plan == "" and calls == []                # deterministic, no model


async def test_model_front_makes_a_plan_call_on_front_backend():
    seen = {}

    async def send(messages, backend, model=None):
        seen["backend"] = backend
        seen["prompt"] = messages[-1]["content"]
        return "1. handle empty\n2. sum"

    cfg = SlotConfig(name="MF", front=FRONT_MODEL, front_backend="deepseek_v4_flash")
    plan = await apply_front(_add(), cfg, send=send)
    assert plan.startswith("1. handle empty")
    assert seen["backend"] == "deepseek_v4_flash"
    assert "count_vowels" not in seen["prompt"]       # only THIS item's visible material
    assert front_context(plan).startswith("Approach plan:")
    assert front_context("") == ""


async def test_model_front_degrades_on_error():
    async def send(messages, backend, model=None):
        raise RuntimeError("front backend down")

    cfg = SlotConfig(name="MF", front=FRONT_MODEL, front_backend="deepseek_v4_flash")
    assert await apply_front(_add(), cfg, send=send) == ""   # hiccup ⇒ passthrough
