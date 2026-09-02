"""MS#4 · P1 notebook-grounded surface — Slice 1 tests (bound provider + F-1 import boundary).

The isolation spine: every retrieval is scoped to the bound notebook_id by construction (no
unfiltered path, no caller override), and no core/surface/ module constructs a raw Qdrant client.
"""
from pathlib import Path

import pytest

from core.surface import NotebookBoundProvider


class _FakeAdapter:
    """Records the filters it was called with (stands in for the read-only LKS ReadAdapter)."""
    def __init__(self):
        self.calls = []

    async def search(self, instance_name, *, query_vector=None, k=10, filters=None):
        self.calls.append({"instance": instance_name, "k": k, "filters": filters})
        return []


async def test_every_search_is_notebook_scoped():
    fake = _FakeAdapter()
    p = NotebookBoundProvider(fake, notebook_id="nb-A", instance_name="wiki")
    await p.vector_search([0.1, 0.2], k=5)
    assert fake.calls[0]["filters"]["notebook_id"] == "nb-A"      # baked in by construction
    assert fake.calls[0]["instance"] == "wiki" and fake.calls[0]["k"] == 5


async def test_caller_cannot_override_or_drop_notebook_id():
    fake = _FakeAdapter()
    p = NotebookBoundProvider(fake, notebook_id="nb-A", instance_name="wiki")
    # a caller tries to escape the partition via extra_filters → forced back to the bound id
    await p.vector_search([0.1], extra_filters={"notebook_id": "nb-OTHER", "topic": "x"})
    f = fake.calls[0]["filters"]
    assert f["notebook_id"] == "nb-A"          # override rejected (set last, wins)
    assert f["topic"] == "x"                    # narrowing is allowed


def test_no_unfiltered_path_exposed():
    # the provider must not expose a method that queries without the notebook_id filter
    methods = {m for m in dir(NotebookBoundProvider) if not m.startswith("_")}
    assert methods == {"vector_search", "notebook_id"}   # only the bound search + the read-only id


def test_construction_rejects_empty_ids():
    with pytest.raises(ValueError):
        NotebookBoundProvider(_FakeAdapter(), notebook_id="", instance_name="wiki")
    with pytest.raises(ValueError):
        NotebookBoundProvider(_FakeAdapter(), notebook_id="nb", instance_name="")


# ── F-1(a): import boundary — no raw Qdrant client construction in core/surface/ ──

def test_surface_never_constructs_raw_qdrant_client():
    surface_dir = Path(__file__).resolve().parents[2] / "core" / "surface"
    offenders = []
    for py in surface_dir.rglob("*.py"):
        if "QdrantClient(" in py.read_text(encoding="utf-8"):
            offenders.append(py.name)
    assert offenders == [], (
        f"core/surface/ must route through the bound provider / injected ReadAdapter, never "
        f"construct a raw Qdrant client (F-1). Offending files: {offenders}"
    )
