"""Test-pipe — cost/token capture (SPEC §8: cost + tokens captured per config).

Wraps a send seam so every model call in a config accumulates a post-hoc
token+USD estimate onto a :class:`CostMeter`. Reuses the proven ``_cost.usd_for``
rate table (the same basis the council cost meter, MS5-C1, uses) so the pipe's
per-config $ number is rate-consistent. Like the council meter this is an ESTIMATE
(the send seam drops real provider ``usage``, COST-1), NOT a metered draw — the
LIVE sweep's real-cost capture (SPEC §10) is Travis's, deferred.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from core.backends import _cost

SendFn = Callable[..., Awaitable[str]]

_INPUT_FRACTION = 0.5  # split the combined estimate 50/50 (mirrors panel.council)


def est_tokens(text: str) -> int:
    """Cheap post-hoc token estimate (~4 chars/token) — the honest text-derived
    basis when no provider usage is exposed."""
    return len(text or "") // 4


@dataclass
class CostMeter:
    """Accumulates token + USD estimates across a config's model calls."""

    tokens: int = 0
    usd: float = 0.0
    calls: int = 0

    def add(self, prompt_tokens: int, response_tokens: int) -> None:
        self.tokens += prompt_tokens + response_tokens
        self.usd = round(
            self.usd + _cost.usd_for(
                input_tokens=prompt_tokens, output_tokens=response_tokens
            ),
            6,
        )
        self.calls += 1


def make_metered_send(inner: SendFn, meter: CostMeter) -> SendFn:
    """Wrap a send seam so each call updates *meter* with an I/O token+USD estimate.

    Matches ``_send_to_backend(messages, backend, model=None)``. Composes UNDER the
    fence wrapper (fence raises before any spend is metered)."""

    async def _send(messages, backend, model=None):
        prompt_text = "".join(
            m.get("content", "") for m in (messages or []) if isinstance(m, dict)
        )
        if model is not None:
            resp = await inner(messages, backend, model)
        else:
            resp = await inner(messages, backend)
        meter.add(est_tokens(prompt_text), est_tokens(str(resp or "")))
        return resp

    return _send
