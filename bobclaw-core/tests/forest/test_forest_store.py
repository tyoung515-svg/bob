"""MS9-F1 — tests for core.forest.store (per-program git-backed ledger repos).

Pins the F1 acceptance criteria on the store surface:
  1. create -> append -> read_ledger_at round-trip returns the appended events.
  2. diff_ledger(base, head) sees the appended events between two commits.
  3. projection_key is stable across clones / byte-identical ledger trees (commit-independent).
Plus: id-safety (no path traversal), fail-loud on bad/empty batches (nothing written), reopen.
"""
from __future__ import annotations

import subprocess

import pytest

from core.forest import events as fe
from core.forest.store import (
    ProgramStore,
    ProgramStoreError,
    create_program,
    exists,
    open_program,
    program_path,
    validate_program_id,
)


def _sample_events():
    return [
        fe.measurement(node_id="H1", metric="uplift", value=0.42, source="testpipe", ts=1000),
        fe.spend(amount_usd=0.03, label="observer tick", ts=1000),
        fe.epoch_open(epoch_id="E1", trigger="delta", ts=1001),
    ]


# ---- acceptance 1: create -> append -> read round-trip ----

def test_create_append_read_roundtrip(tmp_path):
    store = create_program("bobpipe-uplift", root=tmp_path)
    assert isinstance(store, ProgramStore)
    evs = _sample_events()
    store.append_events(evs)
    snap = store.read()
    got_ids = [e["id"] for e in snap["events"]]
    assert got_ids == [e["id"] for e in evs]           # same events, same order, round-tripped
    # payload survives verbatim (the measurement value comes back)
    m = next(e for e in snap["events"] if e["kind"] == "measurement")
    assert m["value"] == 0.42 and m["source"] == "testpipe"


def test_create_yields_a_resolvable_head_immediately(tmp_path):
    # The initial commit (of an empty events.jsonl) MUST land, so HEAD resolves and read/diff/
    # projection_key work before any append. Guards the create_program None-commit failure mode.
    store = create_program("prog", root=tmp_path)
    head = store.head()
    assert isinstance(head, str) and len(head) >= 7
    assert store.read()["ref"] == head          # read_ledger_at resolves the ref
    assert store.events() == []                 # empty ledger, no events yet
    assert store.projection_key().startswith("proj:sha256:")


def test_append_twice_is_cumulative_and_advances_head(tmp_path):
    store = create_program("prog", root=tmp_path)
    h0 = store.head()
    h1 = store.append_events([fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1)])
    h2 = store.append_events([fe.measurement(node_id="H1", metric="m", value=2.0, source="s", ts=2)])
    assert h0 != h1 != h2 and h0 != h2
    assert len(store.events()) == 2


# ---- acceptance 2: diff_ledger sees the appended events ----

def test_diff_ledger_sees_appended_events(tmp_path):
    store = create_program("prog", root=tmp_path)
    base = store.head()
    head = store.append_events(_sample_events())
    d = store.diff(base, head)
    assert d["events_changed"] is True
    assert "ledger/events.jsonl" in (d["added"] + d["modified"])
    # a no-op range reports no change
    assert store.diff(head, head)["events_changed"] is False


# ---- acceptance 3: projection_key commit-independent / stable across clones ----

def test_projection_key_stable_across_clone(tmp_path):
    store = create_program("prog", root=tmp_path)
    store.append_events(_sample_events())
    key = store.projection_key()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(store.repo), str(clone)], check=True, capture_output=True)
    from core.ledger.project import projection_key as pk
    assert pk(str(clone)) == key                       # a clone (new commit objects) has the same key


def test_projection_key_equal_for_byte_identical_trees(tmp_path):
    # Two INDEPENDENT programs with byte-identical ledger content -> equal projection_key
    # (commit-independent, content-addressed).
    evs = _sample_events()
    a = create_program("prog-a", root=tmp_path)
    a.append_events(evs)
    b = create_program("prog-b", root=tmp_path)
    b.append_events(evs)
    assert a.projection_key() == b.projection_key()


def test_projection_key_advances_on_append(tmp_path):
    store = create_program("prog", root=tmp_path)
    k0 = store.projection_key()
    store.append_events(_sample_events())
    assert store.projection_key() != k0


# ---- id safety / fail-loud ----

@pytest.mark.parametrize("bad", ["", "..", "../evil", "a/b", "a\\b", ".hidden", "-flag", "a b"])
def test_validate_program_id_rejects_unsafe(bad):
    with pytest.raises(ProgramStoreError):
        validate_program_id(bad)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b"])
def test_create_program_rejects_traversal_id(bad, tmp_path):
    with pytest.raises(ProgramStoreError):
        create_program(bad, root=tmp_path)


def test_append_empty_raises(tmp_path):
    store = create_program("prog", root=tmp_path)
    with pytest.raises(ProgramStoreError):
        store.append_events([])


def test_append_invalid_event_writes_nothing(tmp_path):
    store = create_program("prog", root=tmp_path)
    head_before = store.head()
    bad = {"kind": "measurement", "ts": 1, "node_id": "H1"}  # missing metric/value/source
    with pytest.raises(ProgramStoreError):
        store.append_events([
            fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1),
            bad,
        ])
    # validation happens before any write/commit -> HEAD unchanged, no events landed
    assert store.head() == head_before
    assert store.events() == []


# ---- create / open lifecycle ----

def test_create_duplicate_raises_and_exist_ok_reopens(tmp_path):
    create_program("prog", root=tmp_path)
    with pytest.raises(ProgramStoreError):
        create_program("prog", root=tmp_path)
    again = create_program("prog", root=tmp_path, exist_ok=True)
    assert isinstance(again, ProgramStore)


def test_open_program_reopens_existing(tmp_path):
    store = create_program("prog", root=tmp_path)
    store.append_events(_sample_events())
    reopened = open_program("prog", root=tmp_path)
    assert [e["id"] for e in reopened.events()] == [e["id"] for e in store.events()]


def test_open_nonexistent_raises(tmp_path):
    with pytest.raises(ProgramStoreError):
        open_program("ghost", root=tmp_path)


def test_exists_reflects_create(tmp_path):
    assert exists("prog", root=tmp_path) is False
    create_program("prog", root=tmp_path)
    assert exists("prog", root=tmp_path) is True


def test_writes_confined_to_root(tmp_path):
    store = create_program("prog", root=tmp_path)
    store.append_events(_sample_events())
    # the repo (and thus every write) lives strictly under the caller-supplied root.
    assert store.repo == program_path("prog", root=tmp_path)
    assert tmp_path in store.repo.parents
