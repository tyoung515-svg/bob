"""MS#4 · T1 — Slice 3 tests (reliability-routing D-2 + quarantine-aware profile cache).

PURE. best_class_for picks the best VERIFIED class per job-shape from tool_trace payloads; the
ProfileCache serves crystallized/probationary profiles but never a quarantined/open one.
(The route.py _select_face fast-path wiring is BLOCKED — SPEC §8 open interaction question.)
"""
from core.mcp_tools import (
    ProfileCache,
    ProfileStatus,
    ShortcutProfile,
    ToolOutcome,
    ToolTrace,
    best_class_for,
)


def _trace(job, cls, success, verdict="entailed"):
    return ToolTrace(
        job_shape=job, tool_id="t", schema_version_hash="h", model_or_position="w",
        capability_class=cls,
        outcome=ToolOutcome(success=success, verify_verdict=verdict),
        ts="2026-07-04T00:00:00Z",
    ).to_l4_node()


# ── D-2 reliability-routing ──────────────────────────────────────────────────

def test_best_class_by_verified_success_rate():
    traces = [
        _trace("plan", "synth_deep", True), _trace("plan", "synth_deep", True),
        _trace("plan", "route_cheap", True), _trace("plan", "route_cheap", False),
        _trace("other", "route_cheap", True),                     # different job-shape → ignored
    ]
    assert best_class_for("plan", traces) == "synth_deep"          # 1.0 > 0.5


def test_gated_reject_does_not_count_as_verified():
    traces = [
        _trace("plan", "cheap", True, verdict="not_entailed"),     # ran but verifier rejected
        _trace("plan", "cheap", True, verdict="not_entailed"),
        _trace("plan", "solid", True, verdict="entailed"),
    ]
    assert best_class_for("plan", traces) == "solid"              # cheap's 'successes' don't count


def test_best_class_min_samples_and_empty():
    traces = [_trace("plan", "cheap", True)]
    assert best_class_for("plan", traces, min_samples=2) is None   # not enough samples
    assert best_class_for("nomatch", traces) is None


# ── profile cache (quarantine-aware fast-path) ───────────────────────────────

def test_cache_serves_servable_not_quarantined():
    crystallized = ShortcutProfile("plan", [("grep", "h")], "route_cheap",
                                   status=ProfileStatus.CRYSTALLIZED)
    quarantined = ShortcutProfile("code", [("grep", "h")], "synth_mid",
                                  status=ProfileStatus.QUARANTINED)
    cache = ProfileCache([crystallized, quarantined])
    assert cache.lookup("plan") is crystallized
    assert cache.lookup("code") is None                           # quarantined → never served
    assert cache.lookup("absent") is None


def test_cache_serves_probationary_but_not_open():
    prob = ShortcutProfile("p", [("t", "h")], "c", status=ProfileStatus.PROBATIONARY)
    opn = ShortcutProfile("o", [("t", "h")], "c", status=ProfileStatus.OPEN)
    cache = ProfileCache([prob, opn])
    assert cache.lookup("p") is prob                              # bypass-but-validate
    assert cache.lookup("o") is None                             # open → not a fast-path
