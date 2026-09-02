"""MS#4 · T1 dynamic-adaptive MCP — Slice 1 tests (telemetry [B] + search_tools [C]).

PURE. Telemetry produces a well-formed tool_trace L4 payload (and hands it to an injected
sink); search_tools ranks a flat index deterministically, suppresses confidence in the output,
and never returns a zero-overlap tool.
"""
from core.mcp_tools import (
    ToolDescriptor,
    ToolOutcome,
    ToolTrace,
    rank_with_confidence,
    record_tool_trace,
    search_tools,
)


# ── [B] telemetry ────────────────────────────────────────────────────────────

def _trace(**over):
    kw = dict(
        job_shape="refactor auth module",
        tool_id="grep",
        schema_version_hash="h1",
        model_or_position="worker",
        capability_class="worker",
        outcome=ToolOutcome(success=True, cost_usd=0.001, latency_ms=42, verify_verdict="entailed"),
        ts="2026-07-04T00:00:00Z",
    )
    kw.update(over)
    return ToolTrace(**kw)


def test_tool_trace_l4_payload_shape():
    payload = _trace().to_l4_node()
    assert payload["node_type"] == "tool_trace"
    assert payload["job_shape"] == "refactor auth module" and payload["tool_id"] == "grep"
    assert payload["outcome"]["success"] is True and payload["outcome"]["latency_ms"] == 42


def test_record_tool_trace_hands_payload_to_sink():
    sink_seen = []
    payload = record_tool_trace(_trace(), sink=sink_seen.append)
    assert sink_seen == [payload]
    assert sink_seen[0]["node_type"] == "tool_trace"
    # no sink ⇒ still returns the payload, records nothing
    assert record_tool_trace(_trace())["tool_id"] == "grep"


# ── [C] search_tools ─────────────────────────────────────────────────────────

_INDEX = [
    ToolDescriptor("grep", "search file contents by regex", ("search", "code"), {"pattern": "str"}, "h1"),
    ToolDescriptor("web_fetch", "fetch a URL over http", ("web", "network"), {"url": "str"}, "h2"),
    ToolDescriptor("read_file", "read a file from disk", ("file", "code", "read"), {"path": "str"}, "h3"),
]


def test_search_tools_ranks_and_suppresses_confidence():
    hits = search_tools(_INDEX, "search code contents", limit=2)
    assert [h["tool_id"] for h in hits][0] == "grep"        # best overlap first
    assert all(set(h) == {"tool_id", "schema"} for h in hits)   # confidence suppressed in output
    assert hits[0]["schema"] == {"pattern": "str"}


def test_search_tools_excludes_zero_overlap_and_respects_limit():
    hits = search_tools(_INDEX, "fetch url http", limit=5)
    ids = [h["tool_id"] for h in hits]
    assert ids[0] == "web_fetch"
    assert "grep" not in ids or "read_file" not in ids        # unrelated tools not force-included
    # a job with no overlap returns nothing (never a spurious tool)
    assert search_tools(_INDEX, "xyzzy plugh", limit=5) == []


def test_rank_with_confidence_exposes_score_for_fallback():
    ranked = rank_with_confidence(_INDEX, "search code", limit=3)
    assert ranked[0][0] == "grep" and ranked[0][1] > 0.0     # (tool_id, confidence) best-first
    assert all(c > 0.0 for _, c in ranked)
