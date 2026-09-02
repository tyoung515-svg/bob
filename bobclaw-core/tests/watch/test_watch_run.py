import pytest
import yaml
from pathlib import Path
from core.watch.run import run_watch, WatchRunResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list:
    with open(FIXTURES_DIR / name) as f:
        return yaml.safe_load(f)


def test_run_watch_with_seen_corpus() -> None:
    seen = load_fixture("seen_corpus.yaml")
    incoming = load_fixture("incoming_corpus.yaml")

    result = run_watch(incoming, seen_corpus=seen, fields=["title", "url"], normalize=True)

    # Concrete assertions against the specification
    assert result.registry_version == 0
    assert len(result.source_ids) == 10
    assert result.new_count == 2
    assert result.seen_count == 3

    # New items must be inc-2 and inc-3 (by id)
    new_ids = [item["id"] for item in result.new]
    assert "inc-2" in new_ids
    assert "inc-3" in new_ids
    assert len(new_ids) == 2

    # Marked items: seen flag per fixture comment
    expected_seen = {
        "inc-1": True,
        "inc-2": False,
        "inc-3": False,
        "inc-4": True,
        "inc-5": True,
    }
    for marked_item in result.marked:
        item_id = marked_item.item["id"]
        assert marked_item.seen == expected_seen[item_id], f"Mismatch for {item_id}"


def test_run_watch_empty_seen_corpus() -> None:
    incoming = load_fixture("incoming_corpus.yaml")

    # Empty list case
    result = run_watch(incoming, seen_corpus=[], fields=["title", "url"], normalize=True)

    # Only intra-batch near-dup inc-4 (of inc-2) is seen
    assert result.new_count == 4
    assert result.seen_count == 1

    new_ids = [item["id"] for item in result.new]
    assert "inc-1" in new_ids
    assert "inc-2" in new_ids
    assert "inc-3" in new_ids
    assert "inc-5" in new_ids
    assert "inc-4" not in new_ids

    expected_seen = {
        "inc-1": False,
        "inc-2": False,
        "inc-3": False,
        "inc-4": True,
        "inc-5": False,
    }
    for marked_item in result.marked:
        item_id = marked_item.item["id"]
        assert marked_item.seen == expected_seen[item_id], f"Mismatch for {item_id}"

    # None case (same behaviour)
    result2 = run_watch(incoming, seen_corpus=None, fields=["title", "url"], normalize=True)
    assert result2.new_count == 4
    assert result2.seen_count == 1

    new_ids2 = [item["id"] for item in result2.new]
    assert "inc-1" in new_ids2
    assert "inc-2" in new_ids2
    assert "inc-3" in new_ids2
    assert "inc-5" in new_ids2
    assert "inc-4" not in new_ids2
