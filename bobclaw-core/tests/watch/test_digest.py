import pytest
import json
import os
import sys
from pathlib import Path

# Add the project root to sys.path if needed (assuming tests are run from project root)
# If not, adjust accordingly. But we assume the module is importable.
from core.watch.digest import (
    WatchEvent,
    AlertThreshold,
    AlertFiring,
    DigestSection,
    DigestArtifact,
    _matches,
    evaluate_thresholds,
    build_digest,
    to_events,
    thresholds_from_dicts,
    digest_from_run,
    persist_digest,
    DIGEST_KIND,
    DIGEST_VERSION,
    SEVERITY_ORDER,
    LIVE_CORPUS_ROOTS,
)

# ---------------------------------------------------------------------------
# 1. WatchEvent / AlertThreshold __post_init__ validation
# ---------------------------------------------------------------------------

def test_watch_event_requires_non_empty_id():
    with pytest.raises(ValueError):
        WatchEvent(id="", title="test")
    with pytest.raises(ValueError):
        WatchEvent(id="  ", title="test")

def test_watch_event_requires_valid_severity():
    with pytest.raises(ValueError, match="severity"):
        WatchEvent(id="e1", title="test", severity="critical")

def test_alert_threshold_requires_non_empty_name():
    with pytest.raises(ValueError):
        AlertThreshold(name="", min_count=1)
    with pytest.raises(ValueError):
        AlertThreshold(name="  ", min_count=1)

def test_alert_threshold_validates_min_severity():
    with pytest.raises(ValueError, match="min_severity"):
        AlertThreshold(name="t", min_severity="critical")

def test_alert_threshold_validates_level():
    with pytest.raises(ValueError, match="level"):
        AlertThreshold(name="t", level="critical")

def test_alert_threshold_validates_min_count():
    with pytest.raises(ValueError, match="min_count"):
        AlertThreshold(name="t", min_count=0)
    with pytest.raises(ValueError, match="min_count"):
        AlertThreshold(name="t", min_count=-1)

def test_valid_watch_event_works():
    e = WatchEvent(id="e1", title="Test", severity="alert")
    assert e.id == "e1"
    assert e.title == "Test"
    assert e.severity == "alert"

def test_valid_alert_threshold_works():
    t = AlertThreshold(name="t1", min_count=2, level="alert")
    assert t.name == "t1"
    assert t.min_count == 2
    assert t.level == "alert"

# ---------------------------------------------------------------------------
# 2. _matches
# ---------------------------------------------------------------------------

def test_matches_category_filter():
    e = WatchEvent(id="e1", title="A", category="update")
    # matching category
    th = AlertThreshold(name="t", category="update")
    assert _matches(e, th) is True
    # non-matching category
    th2 = AlertThreshold(name="t", category="other")
    assert _matches(e, th2) is False

def test_matches_source_filter():
    e = WatchEvent(id="e1", title="A", source="src1")
    th = AlertThreshold(name="t", source="src1")
    assert _matches(e, th) is True
    th2 = AlertThreshold(name="t", source="src2")
    assert _matches(e, th2) is False

def test_matches_min_severity():
    e_info = WatchEvent(id="e1", title="A", severity="info")
    e_notable = WatchEvent(id="e2", title="B", severity="notable")
    e_alert = WatchEvent(id="e3", title="C", severity="alert")
    th_info = AlertThreshold(name="t", min_severity="info")
    assert _matches(e_info, th_info) is True
    assert _matches(e_notable, th_info) is True
    assert _matches(e_alert, th_info) is True
    th_notable = AlertThreshold(name="t", min_severity="notable")
    assert _matches(e_info, th_notable) is False
    assert _matches(e_notable, th_notable) is True
    assert _matches(e_alert, th_notable) is True
    th_alert = AlertThreshold(name="t", min_severity="alert")
    assert _matches(e_info, th_alert) is False
    assert _matches(e_notable, th_alert) is False
    assert _matches(e_alert, th_alert) is True

def test_matches_combined_filters():
    e = WatchEvent(id="e1", title="A", category="cat1", source="src1", severity="notable")
    th = AlertThreshold(name="t", category="cat1", source="src1", min_severity="notable")
    assert _matches(e, th) is True
    th2 = AlertThreshold(name="t", category="cat2", source="src1", min_severity="notable")
    assert _matches(e, th2) is False
    th3 = AlertThreshold(name="t", category="cat1", source="src2", min_severity="notable")
    assert _matches(e, th3) is False
    th4 = AlertThreshold(name="t", category="cat1", source="src1", min_severity="alert")
    assert _matches(e, th4) is False

# ---------------------------------------------------------------------------
# 3. evaluate_thresholds
# ---------------------------------------------------------------------------

def test_evaluate_thresholds_min_count():
    events = [
        WatchEvent(id="e1", title="A", severity="info"),
        WatchEvent(id="e2", title="B", severity="info"),
        WatchEvent(id="e3", title="C", severity="info"),
    ]
    thresholds = [
        AlertThreshold(name="t1", min_count=2, level="notable"),
        AlertThreshold(name="t2", min_count=5, level="alert"),
    ]
    results = evaluate_thresholds(events, thresholds)
    assert len(results) == 1
    assert results[0].threshold == "t1"
    assert results[0].level == "notable"
    assert results[0].count == 3
    assert results[0].event_ids == ("e1", "e2", "e3")

def test_evaluate_thresholds_fires_only_when_ge():
    events = [
        WatchEvent(id="e1", title="A", severity="info"),
        WatchEvent(id="e2", title="B", severity="info"),
    ]
    thresholds = [
        AlertThreshold(name="t1", min_count=2, level="notable"),
        AlertThreshold(name="t2", min_count=3, level="alert"),
    ]
    results = evaluate_thresholds(events, thresholds)
    assert len(results) == 1
    assert results[0].threshold == "t1"

def test_evaluate_thresholds_preserves_event_order():
    events = [
        WatchEvent(id="z", title="Z"),
        WatchEvent(id="a", title="A"),
        WatchEvent(id="m", title="M"),
    ]
    thresholds = [AlertThreshold(name="t", min_count=2)]
    results = evaluate_thresholds(events, thresholds)
    assert results[0].event_ids == ("z", "a", "m")

def test_evaluate_thresholds_firing_order_matches_threshold_order():
    events = [WatchEvent(id="e1", title="A")]
    thresholds = [
        AlertThreshold(name="first", min_count=1),
        AlertThreshold(name="second", min_count=1),
    ]
    results = evaluate_thresholds(events, thresholds)
    assert [r.threshold for r in results] == ["first", "second"]

def test_evaluate_thresholds_omits_non_matching():
    events = [WatchEvent(id="e1", title="A", category="cat1")]
    thresholds = [AlertThreshold(name="t", category="cat2", min_count=1)]
    results = evaluate_thresholds(events, thresholds)
    assert len(results) == 0

# ---------------------------------------------------------------------------
# 4. build_digest
# ---------------------------------------------------------------------------

def test_build_digest_group_by_category_sections_sorted():
    events = [
        WatchEvent(id="e3", title="C", category="z"),
        WatchEvent(id="e1", title="A", category="a"),
        WatchEvent(id="e2", title="B", category="m"),
    ]
    digest = build_digest(events, group_by="category")
    assert len(digest.sections) == 3
    labels = [s.label for s in digest.sections]
    assert labels == ["a", "m", "z"]  # ascending
    # events within section in input order
    for sec in digest.sections:
        if sec.label == "a":
            assert [e.id for e in sec.events] == ["e1"]
        if sec.label == "z":
            assert [e.id for e in sec.events] == ["e3"]
        if sec.label == "m":
            assert [e.id for e in sec.events] == ["e2"]

def test_build_digest_event_count_and_new_count_default():
    events = [WatchEvent(id="e1", title="A"), WatchEvent(id="e2", title="B")]
    digest = build_digest(events, group_by="category")
    assert digest.event_count == 2
    assert digest.new_count == 2

def test_build_digest_new_count_explicit():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events, new_count=0)
    assert digest.new_count == 0

def test_build_digest_group_by_source():
    events = [
        WatchEvent(id="e1", title="A", source="src2"),
        WatchEvent(id="e2", title="B", source="src1"),
        WatchEvent(id="e3", title="C", source="src1"),
    ]
    digest = build_digest(events, group_by="source")
    assert len(digest.sections) == 2
    labels = [s.label for s in digest.sections]
    assert labels == ["src1", "src2"]
    src1_sec = [s for s in digest.sections if s.label == "src1"][0]
    assert [e.id for e in src1_sec.events] == ["e2", "e3"]
    src2_sec = [s for s in digest.sections if s.label == "src2"][0]
    assert [e.id for e in src2_sec.events] == ["e1"]

def test_build_digest_unknown_group_by_raises():
    events = [WatchEvent(id="e1", title="A")]
    with pytest.raises(ValueError, match="group_by"):
        build_digest(events, group_by="unknown")

def test_build_digest_alerts():
    events = [
        WatchEvent(id="e1", title="A", severity="alert"),
        WatchEvent(id="e2", title="B", severity="info"),
    ]
    thresholds = [
        AlertThreshold(name="high", min_severity="alert", min_count=1, level="alert"),
        AlertThreshold(name="any", min_count=2, level="notable"),
    ]
    digest = build_digest(events, thresholds=thresholds)
    # 'high' (min_severity=alert) matches only the alert event e1;
    # 'any' (min_severity defaults to info, min_count=2) matches BOTH e1+e2 -> both fire.
    assert len(digest.alerts) == 2
    assert digest.alerts[0].threshold == "high"
    assert digest.alerts[0].count == 1
    assert digest.alerts[0].event_ids == ("e1",)
    assert digest.alerts[1].threshold == "any"
    assert digest.alerts[1].count == 2
    assert digest.alerts[1].event_ids == ("e1", "e2")

# ---------------------------------------------------------------------------
# 5. to_markdown
# ---------------------------------------------------------------------------

def test_to_markdown_header_with_profile():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events, title="Watch", profile="scout-weekly", new_count=1)
    md = digest.to_markdown()
    assert "# Watch — scout-weekly" in md

def test_to_markdown_header_without_profile():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events, title="Watch", profile=None)
    md = digest.to_markdown()
    assert "# Watch" in md
    assert "—" not in md  # no profile

def test_to_markdown_summary_line():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events, new_count=1)
    md = digest.to_markdown()
    assert "_1 new · 1 events · 0 alerts_" in md

def test_to_markdown_alerts_block_present_when_alerts_fire():
    events = [WatchEvent(id="e1", title="A", severity="alert")]
    thresholds = [AlertThreshold(name="t", min_severity="alert", min_count=1, level="alert")]
    digest = build_digest(events, thresholds=thresholds, new_count=1)
    md = digest.to_markdown()
    assert "## Alerts" in md
    assert "- **t** [alert] — 1 events: e1" in md

def test_to_markdown_alerts_block_absent_when_no_alerts():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events, new_count=0)
    md = digest.to_markdown()
    assert "## Alerts" not in md

def test_to_markdown_event_bullet_with_meta():
    events = [WatchEvent(id="e1", title="Test Event", severity="info", source="src1", url="http://example.com")]
    digest = build_digest(events, new_count=1)
    md = digest.to_markdown()
    assert "- **Test Event** [info] · src1 · http://example.com" in md

def test_to_markdown_event_summary():
    events = [WatchEvent(id="e1", title="A", severity="info", summary="Some summary")]
    digest = build_digest(events, new_count=1)
    md = digest.to_markdown()
    assert "  Some summary" in md  # two spaces

# ---------------------------------------------------------------------------
# 6. to_doc
# ---------------------------------------------------------------------------

def test_to_doc_kind_and_version():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    doc = digest.to_doc()
    assert doc["kind"] == DIGEST_KIND
    assert doc["version"] == DIGEST_VERSION

def test_to_doc_digest_id_prefix():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    doc = digest.to_doc()
    assert doc["digest_id"].startswith("blake3:")

def test_to_doc_digest_id_deterministic():
    events = [WatchEvent(id="e1", title="A")]
    d1 = build_digest(events)
    d2 = build_digest(events)
    assert d1.digest_id == d2.digest_id

def test_to_doc_digest_id_changes_on_event_change():
    events1 = [WatchEvent(id="e1", title="A")]
    events2 = [WatchEvent(id="e1", title="B")]
    d1 = build_digest(events1)
    d2 = build_digest(events2)
    assert d1.digest_id != d2.digest_id

def test_to_doc_sections_shape():
    events = [WatchEvent(id="e1", title="A", url="u", source="s", category="c", severity="info", summary="sum")]
    digest = build_digest(events)
    doc = digest.to_doc()
    assert "sections" in doc
    sec = doc["sections"][0]
    assert sec["label"] == "c"
    evt = sec["events"][0]
    assert evt["id"] == "e1"
    assert evt["title"] == "A"
    assert evt["url"] == "u"
    assert evt["source"] == "s"
    assert evt["category"] == "c"
    assert evt["severity"] == "info"
    assert evt["summary"] == "sum"

def test_to_doc_alerts_shape():
    events = [WatchEvent(id="e1", title="A", severity="alert")]
    thresholds = [AlertThreshold(name="t", min_severity="alert", min_count=1, level="alert")]
    digest = build_digest(events, thresholds=thresholds)
    doc = digest.to_doc()
    assert "alerts" in doc
    alert = doc["alerts"][0]
    assert alert["threshold"] == "t"
    assert alert["level"] == "alert"
    assert alert["count"] == 1
    assert alert["event_ids"] == ["e1"]

def test_to_doc_json_roundtrip():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    doc = digest.to_doc()
    json_str = json.dumps(doc, ensure_ascii=False, sort_keys=False)
    recovered = json.loads(json_str)
    assert recovered == doc

# ---------------------------------------------------------------------------
# 7. to_events
# ---------------------------------------------------------------------------

def test_to_events_basic():
    items = [{"id": "e1", "title": "A"}, {"id": "e2", "title": "B", "url": "http://b"}]
    events = to_events(items)
    assert len(events) == 2
    assert events[0].id == "e1"
    assert events[0].title == "A"
    assert events[0].url is None
    assert events[0].category == "update"
    assert events[0].severity == "info"
    assert events[1].url == "http://b"

def test_to_events_uses_category_severity_from_item():
    items = [{"id": "e1", "title": "A", "category": "custom", "severity": "alert"}]
    events = to_events(items)
    assert events[0].category == "custom"
    assert events[0].severity == "alert"

def test_to_events_defaults():
    items = [{"id": "e1"}]
    events = to_events(items, category="cat", severity="notable")
    assert events[0].category == "cat"
    assert events[0].severity == "notable"

def test_to_events_non_mapping_raises_typeerror():
    with pytest.raises(TypeError):
        to_events([42])

def test_to_events_missing_id_raises_valueerror():
    with pytest.raises(ValueError, match="id"):
        to_events([{"title": "A"}])

# ---------------------------------------------------------------------------
# 8. persist_digest
# ---------------------------------------------------------------------------

def test_persist_digest_roundtrip(tmp_path):
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    path = tmp_path / "digest.json"
    persist_digest(digest, path)
    with open(path, "r", encoding="utf-8") as f:
        written = json.load(f)
    assert written == digest.to_doc()

def test_persist_digest_overwrite_false_raises(tmp_path):
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    path = tmp_path / "digest.json"
    path.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        persist_digest(digest, path, overwrite=False)

def test_persist_digest_overwrite_true_succeeds(tmp_path):
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    path = tmp_path / "digest.json"
    path.write_text("existing", encoding="utf-8")
    persist_digest(digest, path, overwrite=True)
    with open(path, "r", encoding="utf-8") as f:
        written = json.load(f)
    assert written == digest.to_doc()

def test_persist_digest_refuses_live_corpus_root():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    for root in LIVE_CORPUS_ROOTS:
        bad_path = str(root / "whatever.json")
        with pytest.raises(ValueError, match="refusing to write into a live corpus"):
            persist_digest(digest, bad_path)
        # ensure no file was created
        assert not os.path.exists(bad_path)

def test_persist_digest_refuses_within_live_corpus_subdir():
    events = [WatchEvent(id="e1", title="A")]
    digest = build_digest(events)
    bad_path = str(Path("C:/dev/wiki/subdir/digest.json"))
    with pytest.raises(ValueError, match="refusing to write into a live corpus"):
        persist_digest(digest, bad_path)

# ---------------------------------------------------------------------------
# Edge: thresholds_from_dicts (basic smoke)
# ---------------------------------------------------------------------------

def test_thresholds_from_dicts_basic():
    rows = [
        {"name": "t1", "category": "update", "min_count": 2},
        {"name": "t2", "level": "alert"},
    ]
    thresholds = thresholds_from_dicts(rows)
    assert len(thresholds) == 2
    assert thresholds[0].name == "t1"
    assert thresholds[0].category == "update"
    assert thresholds[1].level == "alert"

def test_thresholds_from_dicts_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown"):
        thresholds_from_dicts([{"name": "t", "unknown": "val"}])

# ---------------------------------------------------------------------------
# Edge: digest_from_run over a real W1 WatchRunResult (offline, no mocking)
# ---------------------------------------------------------------------------

def test_digest_from_run_builds_from_result():
    from core.watch.run import WatchRunResult
    items = [
        {"id": "a1", "title": "Alpha", "source": "s1"},
        {"id": "a2", "title": "Beta", "source": "s2"},
    ]
    result = WatchRunResult(
        registry_version=0, source_ids=["s1", "s2"],
        marked=[], new=items, seen_count=0, new_count=2,
    )
    art = digest_from_run(result, group_by="source")
    assert art.new_count == 2
    assert art.event_count == 2
    ids = [e.id for s in art.sections for e in s.events]
    assert sorted(ids) == ["a1", "a2"]
    # W1 items carry no category/severity -> the to_events defaults apply.
    doc = art.to_doc()
    assert all(
        e["category"] == "update" and e["severity"] == "info"
        for s in doc["sections"] for e in s["events"]
    )


# ---------------------------------------------------------------------------
# Edge: threshold boundary + empty digest
# ---------------------------------------------------------------------------

def test_evaluate_thresholds_one_event_min_count_two_does_not_fire():
    events = [WatchEvent(id="only", title="A")]
    thresholds = [AlertThreshold(name="need2", min_count=2)]
    assert evaluate_thresholds(events, thresholds) == []


def test_build_digest_empty_events_renders():
    digest = build_digest([])
    assert digest.sections == ()
    assert digest.alerts == ()
    assert digest.event_count == 0
    assert digest.new_count == 0
    md = digest.to_markdown()
    assert "_0 new · 0 events · 0 alerts_" in md
    assert "## Alerts" not in md
    doc = digest.to_doc()
    assert doc["sections"] == []
    assert doc["digest_id"].startswith("blake3:")
