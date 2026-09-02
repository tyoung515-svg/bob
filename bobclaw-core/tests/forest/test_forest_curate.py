"""MS9-F9 — tests for core.forest.curate (the CURATOR seam).

Primary convergence evidence for the F9 curator (SPEC-RESEARCH-FOREST §2 "Curate" + §7.2). Pins:
  * the **dormancy sweep** (§7.2): an OPEN node with 3 consecutive zero-evidence epochs → dormant;
    evidence resets the counter; supported/refuted nodes NEVER go dormant (verdict precedence);
  * the **archive proposal** over a fully dormant/resolved fixture tree — final synthesis, stop
    observers, repo KEPT, a valid ``archive_proposed`` ledger event, a proposal-only approval item;
    fail-loud on a still-active or empty tree;
  * the **crystallize-out proposal** from SUPPORTED findings — a doc bundle targeting the wiki LKS,
    proposal-only; fail-loud when nothing is supported;
  * **invariant 14 (no live writes):** crystallize-out + archive produce artifacts ONLY, under the
    passed artifact dir; NOTHING auto-applies; with ``artifact_dir=None`` nothing is written anywhere,
    and the simulated live corpora (wiki LKS / corpus) are never touched.

Every proposal path is PURE + deterministic (ids are content-addressed, ts-independent).
"""
from __future__ import annotations

import pytest

import core.forest.hypothesis as hyp
from core.forest.curate import (
    ArchiveProposal,
    CrystallizeProposal,
    CurateError,
    curate,
    is_tree_dormant_or_resolved,
    propose_archive,
    propose_crystallize,
    supported_nodes,
    sweep_dormancy,
)
from core.forest.events import validate_event


# ---------------------------------------------------------------------------
# Fixture-node builders (in-memory HypothesisNodes — no I/O)
# ---------------------------------------------------------------------------
def _supported_node(nid: str, **kw) -> hyp.HypothesisNode:
    node = hyp.make_node(id=nid, **kw)
    for _ in range(9):
        hyp.apply_evidence(node, supporting=True)  # Beta(10,1) → mean ≈ 0.909, obs=9
    assert node.status == hyp.STATUS_SUPPORTED
    return node


def _refuted_node(nid: str, **kw) -> hyp.HypothesisNode:
    node = hyp.make_node(id=nid, **kw)
    for _ in range(9):
        hyp.apply_evidence(node, supporting=False)  # Beta(1,10) → mean ≈ 0.091 ≤ 0.1
    assert node.status == hyp.STATUS_REFUTED
    return node


def _dormant_node(nid: str, **kw) -> hyp.HypothesisNode:
    node = hyp.make_node(id=nid, **kw)
    for _ in range(hyp.DORMANT_EPOCHS):
        sweep_dormancy([node])
    assert node.status == hyp.STATUS_DORMANT
    return node


# ---------------------------------------------------------------------------
# Dormancy sweep (§7.2)
# ---------------------------------------------------------------------------
def test_open_node_goes_dormant_on_third_zero_evidence_epoch():
    node = hyp.make_node(id="n1")
    assert node.status == hyp.STATUS_OPEN

    r1 = sweep_dormancy([node])
    assert node.epochs_without_evidence == 1
    assert node.status == hyp.STATUS_OPEN
    assert r1.newly_dormant == ()
    assert r1.all_inactive is False

    r2 = sweep_dormancy([node])
    assert node.epochs_without_evidence == 2
    assert node.status == hyp.STATUS_OPEN
    assert r2.newly_dormant == ()

    r3 = sweep_dormancy([node])
    assert node.epochs_without_evidence == 3  # DORMANT_EPOCHS
    assert node.status == hyp.STATUS_DORMANT
    assert r3.newly_dormant == ("n1",)
    assert r3.all_inactive is True
    t = r3.transitions[0]
    assert (t.from_status, t.to_status) == (hyp.STATUS_OPEN, hyp.STATUS_DORMANT)
    assert t.changed is True


def test_new_evidence_resets_dormancy_counter():
    node = hyp.make_node(id="n1")
    sweep_dormancy([node])  # eve = 1
    sweep_dormancy([node])  # eve = 2
    r = sweep_dormancy([node], {"n1": 4})  # evidence arrived → reset
    assert node.epochs_without_evidence == 0
    assert node.status == hyp.STATUS_OPEN
    assert r.newly_dormant == ()


def test_supported_and_refuted_never_go_dormant():
    sup = _supported_node("sup")
    ref = _refuted_node("ref")
    for _ in range(5):  # many zero-evidence epochs
        res = sweep_dormancy([sup, ref])
    assert sup.status == hyp.STATUS_SUPPORTED
    assert ref.status == hyp.STATUS_REFUTED
    assert res.newly_dormant == ()
    assert res.all_inactive is True  # supported + refuted are both "inactive"/resolved


def test_sweep_reports_per_node_transitions_in_input_order():
    a = hyp.make_node(id="a")
    b = hyp.make_node(id="b")
    r = sweep_dormancy([a, b], {"a": 2})  # a gets evidence, b does not
    assert [t.node_id for t in r.transitions] == ["a", "b"]
    assert r.transitions[0].new_evidence == 2
    assert r.transitions[1].new_evidence == 0


# ---------------------------------------------------------------------------
# Archive proposal
# ---------------------------------------------------------------------------
def test_propose_archive_over_ready_tree():
    nodes = [
        _supported_node("s1", question="Does slot X uplift?"),
        _refuted_node("r1"),
        _dormant_node("d1"),
    ]
    assert is_tree_dormant_or_resolved(nodes)

    p = propose_archive(nodes, program_id="prog1", ts="2026-07-08T00:00:00Z")
    assert isinstance(p, ArchiveProposal)
    assert p.program_id == "prog1"

    body = p.to_dict()
    assert body["proposal_only"] is True
    assert body["repo_kept"] is True  # the repo is KEPT, never deleted
    assert len(body["stop_observers"]) == 3
    assert "supported" in p.final_synthesis

    ai = p.approval_item()
    assert ai["action_type"] == "forest_archive"
    assert ai["proposal_only"] is True

    ev = p.archive_proposed_event()
    validate_event(ev)  # a valid forest ledger event
    assert ev["kind"] == "archive_proposed"
    assert ev["program_id"] == "prog1"
    assert ev["rationale"]


def test_propose_archive_rejects_still_active_tree():
    nodes = [_supported_node("s1"), hyp.make_node(id="open1")]  # open1 is still active
    with pytest.raises(CurateError, match="dormant/resolved|active"):
        propose_archive(nodes, program_id="prog1", ts="t")


def test_propose_archive_rejects_empty_tree():
    with pytest.raises(CurateError):
        propose_archive([], program_id="prog1", ts="t")


def test_archive_id_is_content_addressed_and_ts_independent():
    p1 = propose_archive([_dormant_node("d1"), _supported_node("s1")], program_id="prog1", ts="t1")
    p2 = propose_archive([_dormant_node("d1"), _supported_node("s1")], program_id="prog1", ts="t2")
    assert p1.archive_id == p2.archive_id  # same tree content, different ts → same id


# ---------------------------------------------------------------------------
# Crystallize-out proposal
# ---------------------------------------------------------------------------
def test_propose_crystallize_from_supported_findings():
    nodes = [
        _supported_node("s1", question="Does slot X uplift?", claim_kind="trend"),
        _refuted_node("r1"),
        _dormant_node("d1"),
    ]
    p = propose_crystallize(nodes, program_id="prog1", ts="t")
    assert isinstance(p, CrystallizeProposal)
    assert p.target == "wiki-lks"
    assert p.lks_namespace == "research_forest"
    # only the SUPPORTED node crystallizes
    assert len(p.docs) == 1
    assert p.docs[0].node_id == "s1"
    assert "Does slot X uplift?" in p.docs[0].title
    assert "SUPPORTED" in p.docs[0].body
    assert "PROPOSAL ONLY" in p.docs[0].body

    ai = p.approval_item()
    assert ai["action_type"] == "forest_crystallize"
    assert ai["proposal_only"] is True
    assert ai["details"]["doc_count"] == 1


def test_propose_crystallize_rejects_when_nothing_supported():
    nodes = [_refuted_node("r1"), _dormant_node("d1")]
    assert supported_nodes(nodes) == []
    with pytest.raises(CurateError, match="supported|crystallize"):
        propose_crystallize(nodes, program_id="prog1", ts="t")


# ---------------------------------------------------------------------------
# Invariant 14 — proposal-only, NO live writes
# ---------------------------------------------------------------------------
def test_inv14_writes_only_under_artifact_dir(tmp_path):
    # Simulated LIVE corpora the fleet must NEVER write.
    wiki = tmp_path / "wiki_lks"
    wiki.mkdir()
    corpus = tmp_path / "live_corpus"
    corpus.mkdir()
    artifacts = tmp_path / "artifacts"  # created by the proposer on demand

    nodes = [_supported_node("s1", question="Q1"), _refuted_node("r1"), _dormant_node("d1")]
    res = curate(nodes, program_id="prog1", ts="2026-07-08T00:00:00Z", artifact_dir=artifacts)

    # Both proposals were produced.
    assert res.archive is not None
    assert res.crystallize is not None

    # Artifacts landed UNDER the artifact dir (proposal JSON + a doc-bundle .md).
    written = list(artifacts.rglob("*"))
    assert any(p.suffix == ".json" for p in written)
    assert any(p.suffix == ".md" for p in written)

    # NOTHING written to the live corpora.
    assert list(wiki.iterdir()) == []
    assert list(corpus.iterdir()) == []

    # Nothing auto-applies: every surfaced item is proposal-only.
    assert res.archive.approval_item()["proposal_only"] is True
    assert res.crystallize.approval_item()["proposal_only"] is True


def test_inv14_artifact_dir_none_writes_nothing_anywhere(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    before = set(tmp_path.rglob("*"))

    nodes = [_supported_node("s1"), _refuted_node("r1"), _dormant_node("d1")]
    res = curate(nodes, program_id="prog1", ts="t", artifact_dir=None)

    assert res.archive is not None
    assert res.crystallize is not None
    # With no artifact_dir, the curator is a pure function: the filesystem is untouched.
    assert set(tmp_path.rglob("*")) == before


def test_individual_proposers_write_nothing_without_artifact_dir(tmp_path):
    before = set(tmp_path.rglob("*"))
    nodes = [_supported_node("s1"), _dormant_node("d1")]
    propose_archive(nodes, program_id="prog1", ts="t")
    propose_crystallize(nodes, program_id="prog1", ts="t")
    assert set(tmp_path.rglob("*")) == before


# ---------------------------------------------------------------------------
# Orchestrator — one full curator pass
# ---------------------------------------------------------------------------
def test_curate_orchestrator_sweeps_then_proposes(tmp_path):
    # A tree with one open node that will still be open after one sweep → NOT archive-ready yet.
    nodes = [_supported_node("s1"), hyp.make_node(id="open1")]
    res = curate(nodes, program_id="prog1", ts="t", artifact_dir=tmp_path / "a")
    # open1 got its first zero-evidence epoch but is still open → no archive proposal.
    assert res.archive is None
    # a supported node exists → crystallize proposal is produced.
    assert res.crystallize is not None
    assert res.sweep.all_inactive is False


def test_curate_result_to_dict_roundtrip():
    nodes = [_supported_node("s1"), _dormant_node("d1")]
    res = curate(nodes, program_id="prog1", ts="t")
    d = res.to_dict()
    assert d["archive"] is not None
    assert d["crystallize"] is not None
    assert d["sweep"]["all_inactive"] is True
