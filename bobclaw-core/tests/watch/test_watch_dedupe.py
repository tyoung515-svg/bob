from __future__ import annotations

import pytest
from core.watch import dedupe


def test_fingerprint_deterministic_and_stable():
    """Call fingerprint twice on the same item; results must be equal."""
    item = {"id": "x1", "title": "Hello World", "url": "http://example.com"}
    fp1 = dedupe.fingerprint(item)
    fp2 = dedupe.fingerprint(item)
    assert fp1 == fp2
    assert fp1.startswith("blake3:")


def test_fingerprint_fields_subset_ignores_other_keys():
    """Two items differing only in 'id' produce identical fingerprint when fields excludes 'id'."""
    item_a = {"id": "a", "title": "Same Title", "url": "http://same.url"}
    item_b = {"id": "b", "title": "Same Title", "url": "http://same.url"}
    fp_a = dedupe.fingerprint(item_a, fields=["title", "url"])
    fp_b = dedupe.fingerprint(item_b, fields=["title", "url"])
    assert fp_a == fp_b


def test_fingerprint_normalize_true_collapses_near_dup():
    """With normalize=True, messily spaced/punctuated items get same fingerprint."""
    item_good = {"title": "DeepSeek V4 Flash benchmark", "url": "https://api-docs.deepseek.com/news/v4-flash"}
    item_messy = {"title": "deepseek   V4   FLASH   benchmark!!", "url": "https://api-docs.deepseek.com/news/v4-flash"}
    fp_good = dedupe.fingerprint(item_good, normalize=True)
    fp_messy = dedupe.fingerprint(item_messy, normalize=True)
    assert fp_good == fp_messy


def test_fingerprint_normalize_false_keeps_difference():
    """With normalize=False, messy and clean items get different fingerprints."""
    item_good = {"title": "DeepSeek V4 Flash benchmark", "url": "https://api-docs.deepseek.com/news/v4-flash"}
    item_messy = {"title": "deepseek   V4   FLASH   benchmark!!", "url": "https://api-docs.deepseek.com/news/v4-flash"}
    fp_good = dedupe.fingerprint(item_good, normalize=False)
    fp_messy = dedupe.fingerprint(item_messy, normalize=False)
    assert fp_good != fp_messy


def test_fingerprint_normalize_default_is_true():
    """Fingerprint with default normalize=True should match explicit True."""
    item = {"title": "  a b c  ", "url": "http://x"}
    fp_default = dedupe.fingerprint(item)
    fp_explicit = dedupe.fingerprint(item, normalize=True)
    assert fp_default == fp_explicit


def test_fingerprint_works_on_non_mapping():
    """Fingerprint accepts str/int and returns a blake3:… string."""
    fp_str = dedupe.fingerprint("hello")
    assert fp_str.startswith("blake3:")
    fp_int = dedupe.fingerprint(42)
    assert fp_int.startswith("blake3:")
    assert fp_str != fp_int


def test_fingerprints_of_returns_set_of_correct_size():
    """fingerprints_of returns a set with one entry per unique fingerprint (after normalization)."""
    items = [
        {"title": "One", "url": "http://one"},
        {"title": "Two", "url": "http://two"},
        {"title": "one", "url": "http://one"},   # duplicate after normalization
    ]
    fps = dedupe.fingerprints_of(items, fields=["title", "url"], normalize=True)
    assert isinstance(fps, set)
    assert len(fps) == 2   # "One"+"http://one" and "Two"+"http://two"


def test_mark_seen_marks_previously_seen():
    """Item whose fingerprint is in the `seen` set gets seen=True."""
    items = [{"title": "Existing", "url": "http://old"}]
    seen = {dedupe.fingerprint(items[0], fields=["title", "url"])}
    marked = dedupe.mark_seen(items, seen, fields=["title", "url"])
    assert len(marked) == 1
    assert marked[0].seen is True


def test_mark_seen_intra_batch_dup_first_occurrence_false():
    """First occurrence of a fingerprint in batch is not seen; later duplicates are seen."""
    items = [
        {"title": "Dup", "url": "http://dup"},
        {"title": "Dup", "url": "http://dup"},
        {"title": "Other", "url": "http://other"},
    ]
    fps = set()  # empty seen
    marked = dedupe.mark_seen(items, fps, fields=["title", "url"])
    assert len(marked) == 3
    assert marked[0].seen is False   # first dup
    assert marked[1].seen is True    # second dup
    assert marked[2].seen is False   # unique


def test_mark_seen_preserves_order():
    """Output list order matches input order."""
    items = [{"title": "z", "url": "http://z"}, {"title": "a", "url": "http://a"}]
    marked = dedupe.mark_seen(items, set(), fields=["title", "url"])
    assert [m.item["title"] for m in marked] == ["z", "a"]


def test_mark_seen_combination_with_seen_and_intra_dup():
    """Combine external seen set and intra-batch duplicates; seen flags correct."""
    ext_item = {"title": "External", "url": "http://ext"}
    ext_fp = dedupe.fingerprint(ext_item, fields=["title", "url"])
    seen = {ext_fp}
    items = [
        {"title": "External", "url": "http://ext"},  # seen = external
        {"title": "New", "url": "http://new"},
        {"title": "External", "url": "http://ext"},  # intra-batch dup of first
    ]
    marked = dedupe.mark_seen(items, seen, fields=["title", "url"])
    assert marked[0].seen is True    # seen externally
    assert marked[1].seen is False   # new
    assert marked[2].seen is True    # intra-batch dup


def test_new_items_returns_only_first_seen():
    """new_items filters marked items where seen is False."""
    item1 = {"title": "Keep", "url": "http://keep"}
    item2 = {"title": "Drop", "url": "http://drop"}
    marked = [
        dedupe.MarkedItem(item=item1, fingerprint="fp", seen=False),
        dedupe.MarkedItem(item=item2, fingerprint="fp", seen=True),
    ]
    new = dedupe.new_items(marked)
    assert new == [item1]


def test_new_items_empty_when_all_seen():
    """If all marked items are seen, new_items returns empty list."""
    marked = [
        dedupe.MarkedItem(item="x", fingerprint="fp", seen=True),
    ]
    assert dedupe.new_items(marked) == []


def test_fingerprint_fields_accepts_single_use_iterable():
    """Regression: fingerprint/fingerprints_of/mark_seen must handle a generator as `fields`.

    Build three content-distinct items (differing in title+url, same extra 'id' key)
    and call dedupe.fingerprints_of with a generator; the result should have size 3,
    not collapse to 2+1 due to generator exhaustion. Also verify mark_seen marks
    all as unseen (no intra-batch duplicates).
    """
    items = [
        {"id": "a", "title": "Alpha", "url": "http://alpha.com"},
        {"id": "b", "title": "Beta", "url": "http://beta.com"},
        {"id": "c", "title": "Gamma", "url": "http://gamma.com"},
    ]
    fields_gen = (f for f in ["title", "url"])

    fps = dedupe.fingerprints_of(items, fields=fields_gen)
    assert isinstance(fps, set)
    assert len(fps) == 3, (
        "Expected 3 distinct fingerprints, but got %d. "
        "This may indicate that the generator was exhausted after the first item, "
        "causing later items to produce identical (empty) fingerprints." % len(fps)
    )

    # Re-create generator because it's consumed; mark_seen must also handle generators
    fields_gen2 = (f for f in ["title", "url"])
    marked = dedupe.mark_seen(items, set(), fields=fields_gen2)
    assert len(marked) == 3
    for m in marked:
        assert m.seen is False, "All items are content-distinct, so none should be marked as seen."
