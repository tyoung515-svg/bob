"""MS9-F7 — tests for core.forest.fork (forks + observational A/B branches).

Pins the F7 accept criteria:
  1. Fork proposal → (test-approved) child ledger with a VERIFIABLE seed projection_key — the child's
     recorded seed key matches the subtree projection recomputed from the parent at the pinned ref.
  2. A/B race fixture → a 2-parent merge=synthesis judgment recorded on the git-DAG.
  3. inv. 14 (proposal-only): propose emits an artifact + approval item; NOTHING auto-applies /
     auto-registers; no write lands outside the artifact dir.
Plus: mandatory "why it can't stay a subtree" rationale; bidirectional provenance on both sides;
explicit pull_from_parent transfer event; fail-closed seed verification; the Curator + RS1 negspace
proposer SEAMS (fixture-injected, no live runs, SPEC §8); pure subtree-projection determinism.
"""
from __future__ import annotations

import dataclasses
import hashlib
import subprocess

import pytest

from core.forest import events as fe
from core.forest.hypothesis import Beta
from core.forest.program import ForestRegistry
from core.forest.store import create_program

from core.forest.fork import (
    APPROVAL_KIND,
    SEED_KEY_PREFIX,
    ABArm,
    CuratorForkProposer,
    ForkError,
    ForkProposal,
    ForkSignal,
    NegspaceForkProposer,
    ab_winner,
    approve_fork,
    arm_from_bools,
    judge_ab_race,
    posteriors_separated,
    projection_seed_key,
    propose_fork,
    read_subtree_projection,
    subtree_projection,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _seed_parent(registry: ForestRegistry, program_id: str = "parent") -> object:
    """Register a parent program and append a couple of node-scoped measurement events.

    Returns the parent ProgramStore. Two nodes: ``n1`` (in the fork subtree) and ``n2`` (out).
    """
    registry.create_program(program_id, question="parent q")
    store = registry.open_store(program_id)
    store.append_events([
        fe.measurement(node_id="n1", metric="uplift", value=0.3, source="testpipe", ts=1),
        fe.measurement(node_id="n1", metric="uplift", value=0.5, source="testpipe", ts=2),
        fe.measurement(node_id="n2", metric="latency", value=12.0, source="flight", ts=3),
    ])
    return store


def _git_parents(repo, sha: str) -> list[str]:
    """Return the parent shas of *sha* (2 parents == a merge commit)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--parents", "-n", "1", sha],
        capture_output=True, text=True, encoding="utf-8",
    )
    toks = out.stdout.split()
    return toks[1:]  # drop the commit itself


def _snapshot(root) -> dict:
    """Content hash of every non-.git file under *root* (proves no ledger/registry write)."""
    import pathlib
    snap = {}
    for p in pathlib.Path(root).rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


# ---------------------------------------------------------------------------
# PURE subtree projection + verifiable seed key
# ---------------------------------------------------------------------------
def test_subtree_projection_filters_to_node_scope():
    ledger = {
        "events": [
            {"id": "e1", "kind": "measurement", "node_id": "n1", "value": 1},
            {"id": "e2", "kind": "measurement", "node_id": "n2", "value": 2},
            {"id": "e3", "kind": "spend", "amount_usd": 1.0},  # program-wide, no node_id
        ],
        "claims": {
            "c1": {"id": "c1", "node_id": "n1", "text": "in"},
            "c2": {"id": "c2", "node_id": "n2", "text": "out"},
        },
    }
    proj = subtree_projection(ledger, ["n1"])
    assert [e["id"] for e in proj["events"]] == ["e1"]        # n2 + program-wide excluded
    assert set(proj["claims"].keys()) == {"c1"}
    assert proj["node_ids"] == ["n1"]


def test_subtree_projection_empty_nodes_raises():
    with pytest.raises(ForkError):
        subtree_projection({"events": [], "claims": {}}, [])


def test_projection_seed_key_is_deterministic_and_prefixed():
    proj = {"node_ids": ["n1"], "events": [{"id": "e1"}], "claims": {}}
    k1 = projection_seed_key(proj)
    k2 = projection_seed_key(dict(proj))
    assert k1 == k2 and k1.startswith(SEED_KEY_PREFIX)
    # a different subtree yields a different key
    assert projection_seed_key({"node_ids": ["n2"], "events": [], "claims": {}}) != k1


def test_read_subtree_projection_matches_pure(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    ref = store.head()
    proj, key = read_subtree_projection(store, ["n1"], ref)
    assert key == projection_seed_key(subtree_projection(store.read(ref), ["n1"]))
    assert all(e.get("node_id") == "n1" for e in proj["events"])


# ---------------------------------------------------------------------------
# Proposal artifact — proposal-only (inv. 14)
# ---------------------------------------------------------------------------
def test_propose_requires_rationale(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    for bad in ("", "   "):
        with pytest.raises(ForkError):
            propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                         rationale=bad, ts=10)


def test_propose_requires_nodes(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    with pytest.raises(ForkError):
        propose_fork(parent_store=store, child_program_id="child", node_ids=[],
                     rationale="why", ts=10)


def test_approval_item_carries_rationale_and_kind(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="the n1 question is now its own program", ts=10, question="child q")
    item = prop.approval_item()
    assert item["action_type"] == APPROVAL_KIND
    assert item["proposal_only"] is True
    assert item["details"]["rationale"] == "the n1 question is now its own program"
    assert item["details"]["seed_key"].startswith(SEED_KEY_PREFIX)
    # the fork_proposed event object validates as a real forest event
    ev = prop.fork_proposed_event()
    fe.validate_event(ev)
    assert ev["kind"] == "fork_proposed" and ev["rationale"]


def test_propose_is_proposal_only_no_writes_outside_artifact_dir(tmp_path):
    """inv. 14: propose emits ONLY an artifact; nothing auto-registers, no parent-ledger write."""
    forest_root = tmp_path / "forest"
    reg = ForestRegistry(root=forest_root)
    store = _seed_parent(reg)
    ref = store.head()

    before = _snapshot(forest_root)
    parent_head_before = store.head()
    artifact_dir = tmp_path / "artifacts"        # OUTSIDE the forest root

    prop = propose_fork(parent_store=store, child_program_id="childX", node_ids=["n1"],
                        rationale="outgrew the tree", ts=10, subtree_ref=ref,
                        artifact_dir=artifact_dir)

    # nothing auto-registered / auto-created
    assert reg.exists("childX") is False
    assert not (forest_root / "childX").exists()
    # no write landed in the forest root (registry.json + parent ledger untouched)
    assert _snapshot(forest_root) == before
    assert store.head() == parent_head_before
    # the ONLY new write is the proposal artifact, inside artifact_dir (filesystem-safe filename)
    assert (artifact_dir / prop.artifact_filename).exists()
    # the default fork id carries a ':' but the artifact filename must be Windows-safe (no ADS)
    assert ":" not in prop.artifact_filename


# ---------------------------------------------------------------------------
# Approval → child seeded with a VERIFIABLE seed key (accept #1)
# ---------------------------------------------------------------------------
def test_approve_seeds_child_with_verifiable_seed_key(tmp_path):
    forest_root = tmp_path / "forest"
    reg = ForestRegistry(root=forest_root)
    store = _seed_parent(reg)
    ref = store.head()
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="n1 outgrew the tree", ts=10, question="child q", subtree_ref=ref)

    result = approve_fork(prop, reg, ts=20, notes="approved by test")

    # child registered + seeded
    assert reg.exists("child")
    child_store = reg.open_store("child")
    child_events = child_store.events()
    kinds = [e["kind"] for e in child_events]
    assert "fork_approved" in kinds and "pull_from_parent" in kinds

    # the child's recorded seed key == the subtree projection recomputed from the parent at ref
    _proj, recomputed = read_subtree_projection(store, prop.node_ids, ref)
    seed_ev = [e for e in child_events if e["kind"] == "fork_approved"][0]
    assert seed_ev["seed_key"] == recomputed == prop.seed_key == result.seed_key
    assert result.seed_verified is True

    # the explicit later-transfer verb points back at the pinned subtree ref
    pull_ev = [e for e in child_events if e["kind"] == "pull_from_parent"][0]
    assert pull_ev["from_ref"] == ref and pull_ev["parent_program"] == "parent"


def test_approve_seeds_child_with_projected_measurement_stream(tmp_path):
    """The child ledger is materialized from the subtree projection (the node-scoped measurements)."""
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)                 # n1 has two uplift measurements (0.3, 0.5)
    ref = store.head()
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="n1 outgrew the tree", ts=10, subtree_ref=ref)
    approve_fork(prop, reg, ts=20)

    child_measurements = [
        e for e in reg.open_store("child").events()
        if e["kind"] == "measurement" and e.get("node_id") == "n1"
    ]
    assert {m["value"] for m in child_measurements} == {0.3, 0.5}   # seeded from the projection
    # the out-of-subtree node (n2) is NOT seeded into the child
    n2 = [e for e in reg.open_store("child").events()
          if e.get("node_id") == "n2"]
    assert n2 == []


def test_artifact_filename_is_sanitized_against_traversal(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    artifact_dir = tmp_path / "arts"
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="why", ts=10, fork_id="../../evil:x", artifact_dir=artifact_dir)
    # the write stays INSIDE artifact_dir; no separator / traversal / ':' escapes
    fname = prop.artifact_filename
    assert "/" not in fname and "\\" not in fname and ":" not in fname and ".." not in fname
    written = list(artifact_dir.glob("*.json"))
    assert written == [artifact_dir / fname]
    assert not (tmp_path / "evil:x.json").exists()


def test_approve_records_provenance_on_both_sides(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    ref = store.head()
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="n1 outgrew the tree", ts=10, subtree_ref=ref)
    approve_fork(prop, reg, ts=20)

    # registry-level bidirectional edge (F1 create_program(parent=...))
    child_rec = reg.get("child")
    parent_rec = reg.get("parent")
    assert child_rec.parent == "parent"
    assert "child" in parent_rec.children

    # ledger-truth-level marker on BOTH ledgers
    parent_kinds = [e["kind"] for e in store.events()]
    assert "fork_approved" in parent_kinds          # parent ledger carries the spawn marker
    child_kinds = [e["kind"] for e in reg.open_store("child").events()]
    assert "fork_approved" in child_kinds


def test_approve_fails_closed_on_seed_mismatch(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    ref = store.head()
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="n1 outgrew the tree", ts=10, subtree_ref=ref)
    tampered = dataclasses.replace(prop, seed_key=f"{SEED_KEY_PREFIX}deadbeef")
    with pytest.raises(ForkError):
        approve_fork(tampered, reg, ts=20)
    # fail-closed: no child was created
    assert reg.exists("child") is False


def test_approve_rejects_unregistered_parent(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    # parent program exists on disk (a raw store) but is NOT registered in this registry
    store = create_program("loner", root=tmp_path / "forest")
    store.append_events([fe.measurement(node_id="n1", metric="m", value=1, source="s", ts=1)])
    ref = store.head()
    prop = propose_fork(parent_store=store, child_program_id="child", node_ids=["n1"],
                        rationale="why", ts=10, subtree_ref=ref)
    with pytest.raises(Exception):  # registry.open_store raises on an unregistered parent
        approve_fork(prop, reg, ts=20)


# ---------------------------------------------------------------------------
# Observational A/B branches → 2-parent merge=synthesis (accept #2)
# ---------------------------------------------------------------------------
def test_arm_from_bools_folds_beta():
    arm = arm_from_bools("A", "n1", [True, True, True, False])
    assert arm.beta.alpha == 4.0 and arm.beta.beta == 2.0 and arm.n_obs == 4
    assert abs(arm.mean - (4 / 6)) < 1e-9


def test_ab_winner_and_separation():
    a = arm_from_bools("A", "n1", [True] * 10)     # mean ~0.917
    b = arm_from_bools("B", "n1", [False] * 10)    # mean ~0.083
    assert posteriors_separated(a, b) is True
    assert ab_winner(a, b).arm_id == "A"


def test_ab_race_records_two_parent_merge_synthesis(tmp_path):
    store = create_program("abprog", root=tmp_path / "forest")
    a = arm_from_bools("armA", "n1", [True] * 10, label="config X")
    b = arm_from_bools("armB", "n1", [False] * 8, label="config Y")

    out = judge_ab_race(store.repo, a, b, date="20260101", slug="ab-n1")

    assert out["judged"] is True
    assert out["winner"] == "armA" and out["delta"] > 0
    assert out["merged"] is True and out["merge_sha"]
    # the merge=synthesis commit is a 2-parent merge (base tip + arm branch tip)
    assert len(_git_parents(store.repo, out["merge_sha"])) == 2
    # the comparison claim landed in the report tree
    landed = store.read("HEAD")["claims"]
    assert out["claim_id"] in landed


def test_ab_race_not_judged_when_close_and_no_deadline(tmp_path):
    store = create_program("abprog2", root=tmp_path / "forest")
    head0 = store.head()
    a = arm_from_bools("armA", "n1", [True])   # both mean ~0.667
    b = arm_from_bools("armB", "n1", [True])
    out = judge_ab_race(store.repo, a, b)
    assert out["judged"] is False
    assert out["merge_sha"] is None
    assert store.head() == head0               # nothing written


def test_ab_race_deadline_forces_judgment_when_close(tmp_path):
    store = create_program("abprog3", root=tmp_path / "forest")
    a = arm_from_bools("armA", "n1", [True, True])
    b = arm_from_bools("armB", "n1", [True, True])   # identical → tie broken by arm_id
    out = judge_ab_race(store.repo, a, b, deadline_reached=True, date="20260101", slug="dl")
    assert out["judged"] is True and out["reason"] == "deadline epoch"
    assert out["winner"] == "armA"                    # deterministic tie-break
    assert len(_git_parents(store.repo, out["merge_sha"])) == 2


# ---------------------------------------------------------------------------
# Proposer SEAMS — fixture-injected, NO live runs (SPEC §8)
# ---------------------------------------------------------------------------
def test_curator_proposer_seam_emits_proposal_only(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    signals = [ForkSignal(child_program_id="curated", node_ids=("n1",),
                          rationale="drifted into its own program", question="curated q")]
    proposer = CuratorForkProposer(lambda: signals)

    proposals = proposer.scan(parent_store=store, ts=10)

    assert len(proposals) == 1
    p = proposals[0]
    assert p.proposer == "curator"
    assert p.rationale.startswith("question outgrew its tree")
    assert isinstance(p, ForkProposal) and p.seed_key.startswith(SEED_KEY_PREFIX)
    # proposal-only: the seam actuated nothing
    assert reg.exists("curated") is False


def test_negspace_proposer_seam_is_fixture_only(tmp_path):
    reg = ForestRegistry(root=tmp_path / "forest")
    store = _seed_parent(reg)
    called = {"n": 0}

    def source():
        called["n"] += 1
        return [ForkSignal(child_program_id="nsp", node_ids=("n1",),
                           rationale="nobody measured this yet")]

    proposer = NegspaceForkProposer(source)
    proposals = proposer.scan(parent_store=store, ts=10)

    assert called["n"] == 1                       # only the injected fixture source was consulted
    assert proposals[0].proposer == "negspace"
    assert proposals[0].rationale.startswith("question nobody asked")
    assert reg.exists("nsp") is False             # NO live run / no actuation


def test_proposer_seam_can_write_artifacts_without_actuating(tmp_path):
    forest_root = tmp_path / "forest"
    reg = ForestRegistry(root=forest_root)
    store = _seed_parent(reg)
    artifact_dir = tmp_path / "proposals"
    before = _snapshot(forest_root)

    proposer = CuratorForkProposer(
        lambda: [ForkSignal(child_program_id="c1", node_ids=("n1",), rationale="r")]
    )
    proposals = proposer.scan(parent_store=store, ts=10, artifact_dir=artifact_dir)

    assert (artifact_dir / proposals[0].artifact_filename).exists()
    assert _snapshot(forest_root) == before       # forest untouched (proposal-only)
