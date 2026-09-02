"""MS#4 · P1 — Slice 3 tests (GroundedRetrieval handle → grounded MVP).

The grounded-only spine: one handle bound to one notebook; writes land in SQLite, reads (text +
vector) are notebook-scoped by construction; crystallize freezes a content digest. Cross-notebook
isolation holds with no synthesis / no council.
"""
import pytest

from core.surface import (
    GroundedRetrieval,
    NotebookBoundProvider,
    SurfaceStore,
    SurfaceStoreError,
)


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    async def search(self, instance_name, *, query_vector=None, k=10, filters=None):
        self.calls.append(filters)
        return []


def _handle(notebook_id="nb-A", adapter=None):
    store = SurfaceStore(":memory:", now=lambda: 1.0)
    store.create_notebook(notebook_id, "nb")
    provider = NotebookBoundProvider(adapter or _FakeAdapter(), notebook_id=notebook_id,
                                     instance_name="wiki")
    return GroundedRetrieval(store, provider, notebook_id), store


def test_handle_requires_matching_provider_binding():
    store = SurfaceStore(":memory:", now=lambda: 1.0)
    store.create_notebook("nb-A", "nb")
    prov_b = NotebookBoundProvider(_FakeAdapter(), notebook_id="nb-B", instance_name="wiki")
    with pytest.raises(SurfaceStoreError):
        GroundedRetrieval(store, prov_b, "nb-A")          # provider bound to a different notebook


def test_writes_and_text_search_scoped():
    h, _ = _handle()
    h.add_source("s1", "https://a", "web")
    h.add_note("n1", "lithium battery weighs 5.4 kg")
    h.add_note("n2", "capacitor stabilizes voltage")
    hits = h.text_search("battery")
    assert [x["note_id"] for x in hits] == ["n1"]


async def test_vector_search_is_notebook_scoped_via_bound_provider():
    adapter = _FakeAdapter()
    h, _ = _handle(adapter=adapter)
    await h.vector_search([0.1, 0.2], k=3)
    assert adapter.calls[0]["notebook_id"] == "nb-A"      # F-1: cannot be dropped

    # even a hostile extra filter can't escape the notebook
    await h.vector_search([0.1], extra_filters={"notebook_id": "nb-OTHER"})
    assert adapter.calls[1]["notebook_id"] == "nb-A"


def test_cross_notebook_isolation():
    # two notebooks in one store; a handle on nb-A never sees nb-B's notes
    store = SurfaceStore(":memory:", now=lambda: 1.0)
    for nb in ("nb-A", "nb-B"):
        store.create_notebook(nb, nb)
    store.add_note("nb-B", "b1", "secret from B")
    prov_a = NotebookBoundProvider(_FakeAdapter(), notebook_id="nb-A", instance_name="wiki")
    h = GroundedRetrieval(store, prov_a, "nb-A")
    assert h.text_search("secret") == []                  # B's note is unreachable from A


def test_crystallize_freezes_digest_deterministically():
    h, store = _handle()
    h.add_note("n1", "alpha")
    d1 = h.crystallize(last_ingest_ts=10.0)
    assert store.get_manifest("nb-A")["crystallized_digest"] == d1
    assert store.get_manifest("nb-A")["last_ingest_ts"] == 10.0
    # same content → same digest; new content → different digest
    h2, _ = _handle()
    h2.add_note("n1", "alpha")
    assert h2.crystallize() == d1
    h.add_note("n2", "beta")
    assert h.crystallize() != d1
