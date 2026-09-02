"""Test-pipe — per-item orchestration (SPEC §2 spines → §7.2 external scorer).

Composes the honesty fence + cost meter + the selected spine + the external scorer
into one scored :class:`ItemResult` — the ``score_one`` unit the chunker (SPEC §6) and
sweep runner (SPEC §8) drive. Build spine → verify(visible) → external scorer(hidden);
council spine → shaped council (no scoring). Network-free: ``raw_send`` + ``scorer`` are
injected (mocked in tests).
"""
from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from core.testpipe.council_spine import run_council_spine
from core.testpipe.fence import PromptRecorder
from core.testpipe.metering import CostMeter
from core.testpipe.scorer import Scorer
from core.testpipe.spine import build_send_factory, run_build_spine, run_visible_verify
from core.testpipe.types import SPINE_COUNCIL, ItemResult, SlotConfig, TestItem

SendFn = Callable[..., Awaitable[str]]


async def run_item(
    item: TestItem,
    config: SlotConfig,
    *,
    raw_send: SendFn,
    scorer: Optional[Scorer] = None,
    workspace_root: Path,
    verify_fn=run_visible_verify,
    sandbox_mode: str = "subprocess",
    recorder: Optional[PromptRecorder] = None,
) -> ItemResult:
    """Score ONE item under ONE config → a full :class:`ItemResult` (SPEC §2, §7.2).

    ``recorder`` (optional) lets a caller — the fence guard test (SPEC §7.3) —
    independently inspect every prompt the item sent. ``scorer`` is required for the
    build spine (it holds the hidden tests); the council spine ignores it."""
    recorder = recorder if recorder is not None else PromptRecorder()
    meter = CostMeter()
    make_send = build_send_factory(raw_send, item, recorder, meter)

    if config.spine == SPINE_COUNCIL:
        cres = await run_council_spine(item, config, make_send=make_send)
        res = ItemResult(item_id=item.id, config_name=config.name,
                        provenance=config.provenance(), impl=cres.answer,
                        error=cres.error)
    else:
        ws = Path(workspace_root) / f"{config.name}-{item.id}"
        res = await run_build_spine(
            item, config, make_send=make_send, workspace=ws,
            verify_fn=verify_fn, sandbox_mode=sandbox_mode,
        )
        if scorer is not None:
            sres = await scorer.score(item, res.impl)
            res.hidden_pass = sres.hidden_pass

    res.cost_usd = meter.usd
    res.tokens = meter.tokens
    return res
