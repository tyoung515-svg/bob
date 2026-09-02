"""Test-pipe — front adapters (SPEC §3, §9 unit 3).

The ``front`` axis is ALWAYS measured in both variants; the PT↔MF delta is the
measured value of the plan pass (SPEC §3).

  * ``passthrough`` — a DETERMINISTIC adapter (item → request, no model call).
  * ``model`` — a reason/plan pass wrapping ``core/nodes/decompose.py``'s injectable
    LLM seam (``_default_call_llm``), backend-slotted. Its output becomes extra
    context prepended to the worker prompt — never replacing the visible spec.

Fence-safe by construction: the plan pass is fed ONLY the visible prompt + visible
tests, so it can never see (or emit) a hidden payload (SPEC §7).
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from core.testpipe.types import FRONT_MODEL, FRONT_PASSTHROUGH, SlotConfig, TestItem

SendFn = Callable[..., Awaitable[str]]


def _visible_block(item: TestItem) -> str:
    tests = "\n".join(item.visible_tests) or "(no visible examples)"
    return f"{item.prompt}\n\nVisible examples:\n{tests}"


async def apply_front(
    item: TestItem,
    config: SlotConfig,
    *,
    send: Optional[SendFn] = None,
) -> str:
    """Run the front slot for *item*; return the plan text to prepend to the worker
    prompt (``""`` for passthrough).

    ``passthrough`` makes NO model call (deterministic). ``model`` sends a reason/plan
    prompt over the VISIBLE material via *send* (the fenced+metered seam) on
    ``config.front_backend`` and returns the reasoning. A failed/empty plan pass
    degrades to ``""`` (the worker still runs) — a hiccup never sinks the item."""
    if config.front != FRONT_MODEL:
        return ""
    if send is None:
        return ""
    prompt = (
        "You are planning an implementation. Think step by step about the approach "
        "for the function below, then state a short plan (no code).\n\n"
        f"{_visible_block(item)}"
    )
    try:
        plan = await send([{"role": "user", "content": prompt}], config.front_backend)
    except Exception:  # noqa: BLE001 — a front hiccup degrades to passthrough
        return ""
    return (plan or "").strip()


def front_context(plan_text: str) -> str:
    """Format a non-empty plan into a worker-prompt preamble (empty ⇒ '')."""
    if not plan_text:
        return ""
    return f"Approach plan:\n{plan_text}\n\n"
