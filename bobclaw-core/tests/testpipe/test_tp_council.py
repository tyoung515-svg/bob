"""Test-pipe — council spine harness (SPEC §2, §9 unit 6). Network-free (mocked seats)."""
from __future__ import annotations

from core.testpipe.council_spine import run_council_spine
from core.testpipe.fence import PromptRecorder, make_fenced_send
from core.testpipe.metering import CostMeter, make_metered_send
from core.testpipe.types import SPINE_COUNCIL, SlotConfig, TestItem


def _reasoning_item():
    return TestItem(id="cq1", prompt="Should the cache be write-through or write-back?",
                    entry_point="", visible_tests=(), hidden_tests=())


def _make_send(item, rec, meter):
    async def raw(messages, backend, model=None):
        return f"[{backend}] position on the question"
    def make(stage):
        return make_fenced_send(make_metered_send(raw, meter), item, recorder=rec, stage=stage)
    return make


async def test_fusion_reuses_panel_dispatch_and_runs_seats():
    it = _reasoning_item()
    rec, meter = PromptRecorder(), CostMeter()
    cfg = SlotConfig(name="council_fusion", spine=SPINE_COUNCIL, shape="fusion")
    res = await run_council_spine(it, cfg, make_send=_make_send(it, rec, meter),
                                  seats=["framer", "stress", "synth"])
    assert res.shape == "fusion"
    assert len(res.seats) == 3 and res.answer            # synthesized from the seats
    assert meter.calls == 3                              # one call per seat
    assert rec.leaks_for(it) == []                       # fence clean on the council spine too


async def test_sequential_chains_seats():
    it = _reasoning_item()
    rec, meter = PromptRecorder(), CostMeter()
    cfg = SlotConfig(name="council_sequential", spine=SPINE_COUNCIL, shape="sequential",
                     worker="deepseek_v4_flash")
    res = await run_council_spine(it, cfg, make_send=_make_send(it, rec, meter),
                                  seats=["framer", "stress", "synth"])
    assert res.shape == "sequential" and res.answer
    assert len(res.seats) == 3
    # the chain feeds each seat the prior answer (delta-only): 2nd+ prompts carry "PRIOR VOICE"
    prior_prompts = [p["messages"][-1]["content"] for p in rec.prompts]
    assert any("PRIOR VOICE" in c for c in prior_prompts)


async def test_debate_selects_the_shape_and_runs():
    it = _reasoning_item()
    rec, meter = PromptRecorder(), CostMeter()
    cfg = SlotConfig(name="council_debate", spine=SPINE_COUNCIL, shape="debate")
    res = await run_council_spine(it, cfg, make_send=_make_send(it, rec, meter),
                                  seats=["framer", "stress"])
    assert res.shape == "debate" and res.seats and res.answer
