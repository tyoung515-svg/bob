"""MS#4 · P1 — Slice 2 tests (SQLite object graph + surface_manifest).

Deterministic (injected clock, in-memory DB). Notebooks/sources persist, the manifest is seeded
on notebook creation, source_count is derived, and upsert_manifest is partial (COALESCE).
"""
import pytest

from core.surface import SurfaceStore, SurfaceStoreError


def _store():
    ticks = iter(range(1000, 2000))
    return SurfaceStore(":memory:", now=lambda: float(next(ticks)))


def test_create_notebook_seeds_manifest():
    s = _store()
    s.create_notebook("nb-A", "Battery research", embedding_model="granite-311m",
                      embedding_model_sha256="abc")
    nb = s.get_notebook("nb-A")
    assert nb["name"] == "Battery research" and nb["embedding_model_sha256"] == "abc"
    m = s.get_manifest("nb-A")
    assert m is not None and m["source_count"] == 0        # manifest seeded


def test_duplicate_notebook_raises():
    s = _store()
    s.create_notebook("nb-A", "x")
    with pytest.raises(SurfaceStoreError):
        s.create_notebook("nb-A", "y")


def test_add_source_updates_derived_count():
    s = _store()
    s.create_notebook("nb-A", "x")
    s.add_source("nb-A", "src1", "https://a", "web")
    s.add_source("nb-A", "src2", "file://b.pdf", "pdf")
    assert s.source_count("nb-A") == 2
    assert s.get_manifest("nb-A")["source_count"] == 2     # manifest kept in sync


def test_add_source_unknown_notebook_raises():
    s = _store()
    with pytest.raises(SurfaceStoreError):
        s.add_source("ghost", "src1", "u", "web")


def test_upsert_manifest_is_partial():
    s = _store()
    s.create_notebook("nb-A", "x")
    s.upsert_manifest("nb-A", centroid=b"\x00\x01", crystallized_digest="d1")
    s.upsert_manifest("nb-A", last_ingest_ts=42.0)          # only ts changes
    m = s.get_manifest("nb-A")
    assert m["centroid"] == b"\x00\x01"                     # preserved (COALESCE)
    assert m["crystallized_digest"] == "d1" and m["last_ingest_ts"] == 42.0


def test_upsert_manifest_unknown_notebook_raises():
    s = _store()
    with pytest.raises(SurfaceStoreError):
        s.upsert_manifest("ghost", crystallized_digest="d")
