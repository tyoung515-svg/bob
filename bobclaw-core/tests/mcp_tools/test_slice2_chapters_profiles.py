"""MS#4 · T1 — Slice 2 tests (chapter index [A] + shortcut profiles [D]/D-4/F1).

PURE. Coarse-to-fine chapter retrieval, the F1-hardened promotion gate (≥2/3 heterogeneous +
a deterministic check), and schema-drift quarantine.
"""
from core.mcp_tools import (
    Chapter,
    ProfileStatus,
    ShortcutProfile,
    ToolDescriptor,
    Vote,
    chapter_retrieve,
    check_schema_drift,
    coarse_to_fine,
    consensus_ok,
    promote,
    seed_probationary,
)

_INDEX = [
    ToolDescriptor("grep", "search file contents by regex", ("search", "code"), {"p": "str"}, "h1"),
    ToolDescriptor("read_file", "read a file from disk", ("file", "code"), {"path": "str"}, "h2"),
    ToolDescriptor("web_fetch", "fetch a url over http", ("web",), {"url": "str"}, "h3"),
]
_CHAPTERS = [
    Chapter("code_nav", ("code", "search", "file"), ("grep", "read_file"), "navigate code"),
    Chapter("web_io", ("web", "http"), ("web_fetch",), "web access"),
]


# ── [A] chapter index ────────────────────────────────────────────────────────

def test_chapter_retrieve_ranks_by_overlap():
    hits = chapter_retrieve(_CHAPTERS, "search code file", k=2)
    assert hits[0].chapter_id == "code_nav"
    assert chapter_retrieve(_CHAPTERS, "xyzzy", k=2) == []      # zero overlap → nothing


def test_coarse_to_fine_confines_to_chapter_tools():
    hits = coarse_to_fine(_CHAPTERS, _INDEX, "search code", k_chapters=1, limit=5)
    ids = [h["tool_id"] for h in hits]
    assert "grep" in ids and "web_fetch" not in ids            # confined to the code_nav chapter
    assert all(set(h) == {"tool_id", "schema"} for h in hits)  # confidence suppressed
    assert coarse_to_fine(_CHAPTERS, _INDEX, "xyzzy", limit=5) == []  # no chapter → widen (empty)


# ── [D]/D-4/F1 promotion gate ────────────────────────────────────────────────

def test_consensus_requires_ratio_determinism_and_heterogeneity():
    # 3/3 pass, heterogeneous, one deterministic → promote
    assert consensus_ok([
        Vote("deepseek", True), Vote("glm", True, deterministic=True), Vote("minimax", True)]) is True
    # ≥2/3 but NO deterministic check → reject (F1)
    assert consensus_ok([Vote("deepseek", True), Vote("glm", True), Vote("minimax", False)]) is False
    # all pass + deterministic but HOMOGENEOUS (one source) → reject (N=1 in disguise)
    assert consensus_ok([Vote("glm", True, deterministic=True), Vote("glm", True)]) is False
    # below 2/3 → reject
    assert consensus_ok([Vote("a", True, deterministic=True), Vote("b", False), Vote("c", False)]) is False


def test_promote_only_from_probationary():
    p = seed_probationary("plan-intent", [("grep", "h1")], "route_cheap")
    assert p.status is ProfileStatus.PROBATIONARY
    promote(p, [Vote("deepseek", True), Vote("check", True, deterministic=True), Vote("minimax", True)])
    assert p.status is ProfileStatus.CRYSTALLIZED
    # a weak consensus leaves it probationary
    p2 = seed_probationary("code-shaped", [("grep", "h1")], "synth_mid")
    promote(p2, [Vote("glm", True), Vote("glm", True)])         # homogeneous
    assert p2.status is ProfileStatus.PROBATIONARY


# ── schema-drift quarantine ──────────────────────────────────────────────────

def test_schema_drift_quarantines():
    p = ShortcutProfile("plan", [("grep", "h1"), ("read_file", "h2")], "route_cheap",
                        status=ProfileStatus.CRYSTALLIZED)
    assert check_schema_drift(p, {"grep": "h1", "read_file": "h2"}) is False   # in sync
    assert p.status is ProfileStatus.CRYSTALLIZED
    assert check_schema_drift(p, {"grep": "h1", "read_file": "CHANGED"}) is True
    assert p.status is ProfileStatus.QUARANTINED                                # never silently used
