"""MS8-W2 golden test — fixture events -> golden digest artifact (§6 W2 acceptance).

Data-bound (L2-authored): loads the committed fixture events + thresholds, rebuilds the
digest, and pins BOTH rendered forms byte/shape-exact against the frozen goldens
(digest_golden.md, digest_golden.json). Also exercises the REAL W1 -> W2 path
(run_watch -> WatchRunResult -> digest_from_run) to prove read-only W1 consumption, and the
inv. 2 persist guard against a live-corpus path. Offline (inv. 11): no network, no DB.
"""
from pathlib import Path
import json

import yaml
import pytest

from core.watch.digest import (
    to_events,
    thresholds_from_dicts,
    build_digest,
    digest_from_run,
    persist_digest,
    LIVE_CORPUS_ROOTS,
)
from core.watch.run import run_watch

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def _build_from_fixtures():
    events = to_events(_load("digest_events.yaml"))
    thresholds = thresholds_from_dicts(_load("digest_thresholds.yaml"))
    return build_digest(
        events,
        thresholds=thresholds,
        title="Watch digest",
        profile="scout-weekly",
        group_by="category",
        new_count=len(events),
    )


def test_golden_markdown_exact():
    """to_markdown() matches the frozen golden byte-for-byte (LF-normalized)."""
    art = _build_from_fixtures()
    golden = (FIX / "digest_golden.md").read_text(encoding="utf-8")
    assert art.to_markdown() == golden


def test_golden_doc_exact():
    """to_doc() matches the frozen golden JSON exactly (incl. the content-addressed digest_id)."""
    art = _build_from_fixtures()
    golden = json.loads((FIX / "digest_golden.json").read_text(encoding="utf-8"))
    assert art.to_doc() == golden
    # digest_id is the LKS blake3 content fingerprint over the doc body.
    assert golden["digest_id"].startswith("blake3:")
    assert art.digest_id == golden["digest_id"]


def test_digest_id_is_lks_blake3_fingerprint():
    """digest_id is EXACTLY blake3_hex(canonical_json(doc-without-id)) — the LKS content
    fingerprint, not any static/trivial hash. Recompute it independently and assert equality."""
    from core.memory._hashing import blake3_hex, canonical_json

    art = _build_from_fixtures()
    doc = art.to_doc()
    body = {k: v for k, v in doc.items() if k != "digest_id"}
    assert doc["digest_id"] == blake3_hex(canonical_json(body))
    assert doc["digest_id"].startswith("blake3:")


def test_golden_alert_semantics():
    """Concrete acceptance: the two firing thresholds fire with exact ids; the
    non-matching one (security-watch, category=security, no such events) is OMITTED."""
    art = _build_from_fixtures()
    fired = {a.threshold: a for a in art.alerts}
    assert set(fired) == {"pricing-alert", "any-notable"}
    assert "security-watch" not in fired  # 0 matches -> not fired
    assert fired["pricing-alert"].level == "alert"
    assert fired["pricing-alert"].count == 2
    assert fired["pricing-alert"].event_ids == ("ev-1", "ev-4")
    assert fired["any-notable"].level == "notable"
    assert fired["any-notable"].event_ids == ("ev-1", "ev-2", "ev-4")
    # firing order == threshold file order
    assert [a.threshold for a in art.alerts] == ["pricing-alert", "any-notable"]


def test_golden_sections_sorted_and_grouped():
    """Sections grouped by category, labels sorted asc, events in input order."""
    art = _build_from_fixtures()
    assert [s.label for s in art.sections] == ["community", "pricing", "registry", "release"]
    pricing = next(s for s in art.sections if s.label == "pricing")
    assert [e.id for e in pricing.events] == ["ev-1", "ev-4"]
    assert art.event_count == 6
    assert art.new_count == 6


def test_digest_from_run_consumes_w1_result():
    """REAL W1 -> W2 path: run_watch over the W1 fixture corpora produces a WatchRunResult;
    digest_from_run turns its NEW items into a digest. Proves read-only W1 consumption."""
    seen = _load("seen_corpus.yaml")
    incoming = _load("incoming_corpus.yaml")
    result = run_watch(incoming, seen_corpus=seen, fields=["title", "url"], normalize=True)
    # W1 acceptance: new items are inc-2, inc-3 (see RESULTS-W1).
    assert result.new_count == 2

    art = digest_from_run(
        result,
        title="Watch digest",
        profile="scout-weekly",
        group_by="source",
    )
    # new_count is carried through from the run result.
    assert art.new_count == 2
    # every NEW item id appears in exactly one section.
    doc = art.to_doc()
    seen_ids = [e["id"] for s in doc["sections"] for e in s["events"]]
    assert sorted(seen_ids) == ["inc-2", "inc-3"]
    # default classification (no category/severity on W1 items) -> "update"/"info".
    for s in doc["sections"]:
        for e in s["events"]:
            assert e["category"] == "update"
            assert e["severity"] == "info"
    # doc is JSON-serializable and content-addressed.
    assert doc["digest_id"].startswith("blake3:")
    json.dumps(doc)  # no raise


def test_persist_guard_refuses_every_live_corpus_root(tmp_path):
    """inv. 2: persist_digest writes to a TEST path but refuses any live-corpus root,
    creating no file there. Also confirms a normal round-trip to tmp_path."""
    art = _build_from_fixtures()
    # normal persist to a test dir round-trips.
    out = persist_digest(art, tmp_path / "sub" / "digest.json")
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8")) == art.to_doc()
    # every live-corpus root (and a nested path under one) is refused, no file created.
    for root in LIVE_CORPUS_ROOTS:
        target = root / "watch" / "digest.json"
        with pytest.raises(ValueError, match="live corpus"):
            persist_digest(art, target)
        assert not target.exists()
