"""T1 dynamic-adaptive MCP — tool-call TELEMETRY (MS#4 · Slice 1, workstream [B]).

Telemetry-first: every tool call records the triple **job → tool → model/position → outcome**
so the learned layers (chapters, shortcut profiles) later train on real signal, never on the
model's say-so. This is the bobclaw-side schema + recorder; crystallization to an LKS
``node_type:"tool_trace"`` L4 node is a SEAM (an injected sink) — the LKS-side crystallizer is a
cross-repo follow-up. PURE (the clock, the sink, and the embedding are all injected).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class ToolOutcome:
    success: bool
    cost_usd: float = 0.0
    latency_ms: int = 0
    verify_verdict: Optional[str] = None   # the core/verify verdict when the call was gated


@dataclass(frozen=True)
class ToolTrace:
    """One tool-call trace (SPEC §6). ``job_shape_embedding`` is filled LKS-side (kept optional
    here so the recorder stays model-free)."""
    job_shape: str
    tool_id: str
    schema_version_hash: str
    model_or_position: str
    capability_class: str
    outcome: ToolOutcome
    ts: str                                # ISO-8601, injected (no clock in pure code)
    job_shape_embedding: Optional[list] = field(default=None)

    def to_l4_node(self) -> dict:
        """The L4 ``tool_trace`` node payload (what the LKS crystallizer will ingest)."""
        d = asdict(self)
        d["node_type"] = "tool_trace"
        return d


# A sink is any callable that persists a trace's L4 payload (LKS crystallizer, a queue, a test list).
TraceSink = Callable[[dict], None]


def record_tool_trace(trace: ToolTrace, *, sink: Optional[TraceSink] = None) -> dict:
    """Emit a tool trace as an L4 ``tool_trace`` node payload, handing it to ``sink`` if given.
    Returns the payload (so a caller/test can inspect it). No sink ⇒ just build the payload."""
    payload = trace.to_l4_node()
    if sink is not None:
        sink(payload)
    return payload


__all__ = ["ToolOutcome", "ToolTrace", "TraceSink", "record_tool_trace"]
