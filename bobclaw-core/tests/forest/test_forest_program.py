"""MS9-F1 — tests for core.forest.program (the forest registry + provenance DAG).

Pins: program create/list/get; duplicate + missing fail-loud; BIDIRECTIONAL fork-provenance edges;
JSON persistence across registry instances; created_at_ref == the ledger's initial commit; the
registry catalogs metadata but never writes measurement events (that stays store.append_events).
"""
from __future__ import annotations

import json

import pytest

from core.forest import events as fe
from core.forest.program import ForestRegistry, ProgramRecord, ProgramRegistryError
from core.forest.store import ProgramStore


def test_create_get_list(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    rec = reg.create_program("prog-a", question="What contributes uplift?", metadata={"owner": "travis"})
    assert isinstance(rec, ProgramRecord)
    assert rec.id == "prog-a" and rec.question == "What contributes uplift?"
    assert rec.metadata == {"owner": "travis"}
    got = reg.get("prog-a")
    assert got.id == "prog-a"
    reg.create_program("prog-b")
    assert [r.id for r in reg.list()] == ["prog-a", "prog-b"]   # sorted, deterministic


def test_get_missing_raises(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    with pytest.raises(ProgramRegistryError):
        reg.get("ghost")


def test_duplicate_raises_and_exist_ok_returns_existing(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    a = reg.create_program("prog-a")
    with pytest.raises(ProgramRegistryError):
        reg.create_program("prog-a")
    again = reg.create_program("prog-a", exist_ok=True)
    assert again.id == a.id


def test_created_at_ref_is_the_ledger_head(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    rec = reg.create_program("prog-a")
    store = reg.open_store("prog-a")
    assert rec.created_at_ref == store.head()          # the initial ledger commit sha
    assert isinstance(rec.created_at_ref, str) and len(rec.created_at_ref) >= 7


# ---- provenance (the fork seam F7 builds on) ----

def test_provenance_edge_is_bidirectional(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("parent")
    child = reg.create_program("child", parent="parent")
    assert child.parent == "parent"                    # child -> parent
    assert "child" in reg.get("parent").children       # parent -> child
    assert reg.children("parent") == ["child"]


def test_children_sorted_and_deduped(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("parent")
    reg.create_program("z-child", parent="parent")
    reg.create_program("a-child", parent="parent")
    assert reg.children("parent") == ["a-child", "z-child"]


def test_parent_not_registered_raises(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    with pytest.raises(ProgramRegistryError):
        reg.create_program("child", parent="nope")


# ---- exist_ok must not bypass the provenance invariants (audit F1-r5) ----

def test_exist_ok_with_unregistered_parent_raises(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("child")
    # the idempotent early-return must STILL enforce the parent guard (F1-r5 finding 1)
    with pytest.raises(ProgramRegistryError):
        reg.create_program("child", parent="ghost", exist_ok=True)


def test_exist_ok_refuses_to_reparent(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("p1")
    reg.create_program("p2")
    reg.create_program("child", parent="p1")
    # re-registering with a DIFFERENT parent must raise, never silently desync the DAG (F1-r5 finding 2)
    with pytest.raises(ProgramRegistryError):
        reg.create_program("child", parent="p2", exist_ok=True)
    assert reg.get("child").parent == "p1"
    assert reg.children("p1") == ["child"]
    assert reg.children("p2") == []


def test_exist_ok_repairs_half_written_edge(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("parent")
    reg.create_program("child", parent="parent")
    # simulate a crash that wrote child.parent but not parent.children
    reg.get("parent").children.clear()
    assert reg.children("parent") == []
    # idempotent re-registration with the SAME parent repairs the bidirectional edge, and persists
    reg.create_program("child", parent="parent", exist_ok=True)
    assert reg.children("parent") == ["child"]
    assert ForestRegistry(root=tmp_path).children("parent") == ["child"]


def test_invalid_program_id_rejected(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    with pytest.raises(Exception):  # ProgramStoreError (a ForestError subclass) via validate_program_id
        reg.create_program("../evil")


# ---- persistence ----

def test_persistence_across_registry_instances(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("parent")
    reg.create_program("child", parent="parent")
    # a fresh registry over the same root re-reads registry.json from disk
    reg2 = ForestRegistry(root=tmp_path)
    assert [r.id for r in reg2.list()] == ["child", "parent"]
    assert reg2.get("child").parent == "parent"
    assert reg2.children("parent") == ["child"]


def test_reload_drops_cache(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("prog-a")
    # a second registry writes a new program; the first must see it after reload()
    other = ForestRegistry(root=tmp_path)
    other.create_program("prog-b")
    reg.reload()
    assert reg.exists("prog-b")


def test_registry_json_is_valid_and_atomic(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("prog-a")
    idx = tmp_path / "registry.json"
    assert idx.exists()
    data = json.loads(idx.read_text(encoding="utf-8"))
    assert "prog-a" in data["programs"]
    # no stray temp file left behind by the atomic write
    assert not (tmp_path / "registry.tmp").exists()


# ---- registry is a catalog; truth stays in the git ledger ----

def test_open_store_gives_a_working_ledger(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("prog-a")
    store = reg.open_store("prog-a")
    assert isinstance(store, ProgramStore)
    store.append_events([fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1)])
    assert len(store.events()) == 1
    # appending measurement events does NOT mutate the registry catalog
    assert reg.get("prog-a").metadata == {}


def test_open_store_missing_raises(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    with pytest.raises(ProgramRegistryError):
        reg.open_store("ghost")


# ---- crash-consistency: an orphan on-disk ledger (registry record lost) ----

def _orphan_ledger(tmp_path, program_id, *, with_append=False):
    """Simulate a crash between store-create and registry save: a ledger repo exists on disk with NO
    registry record. Done by creating the ledger directly via the store (bypassing the registry)."""
    from core.forest.store import create_program as store_create
    from core.forest import events as fe
    st = store_create(program_id, root=tmp_path)
    if with_append:
        st.append_events([fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1)])
    return st


def test_orphan_ledger_without_exist_ok_raises_recoverably(tmp_path):
    _orphan_ledger(tmp_path, "prog")
    reg = ForestRegistry(root=tmp_path)
    assert reg.exists("prog") is False           # registry has no record (crash lost it)
    with pytest.raises(ProgramRegistryError):    # a clear, recoverable error — not a wedge
        reg.create_program("prog")


def test_orphan_ledger_is_adopted_with_exist_ok(tmp_path):
    st = _orphan_ledger(tmp_path, "prog", with_append=True)
    root_commit = st.root_ref()
    reg = ForestRegistry(root=tmp_path)
    rec = reg.create_program("prog", question="adopted", exist_ok=True)
    assert reg.exists("prog") is True
    # created_at_ref is the ledger's ROOT commit (creation), NOT the current head after appends.
    assert rec.created_at_ref == root_commit
    assert rec.created_at_ref != st.head()
    # the adopted ledger is intact and readable through the registry
    assert len(reg.open_store("prog").events()) == 1


def test_root_ref_is_stable_across_appends(tmp_path):
    from core.forest.store import create_program as store_create
    from core.forest import events as fe
    st = store_create("prog", root=tmp_path)
    root = st.root_ref()
    assert root == st.head()                      # fresh repo: root == head
    st.append_events([fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1)])
    assert st.root_ref() == root                  # root is stable; head has advanced
    assert st.head() != root
