"""MS9-F4 — tests for core.forest.epoch (the hypothesize EPOCH loop + council synthesis).

Pins the F4 accept criteria:
  1. A MOCKED-backend epoch runs end-to-end deterministically: seed -> (mock research arm produces
     claims) -> verified claims append as ledger events -> merge=synthesis commit. No live fleet call
     (the research arm is an injected async mock). Deterministic: two fresh runs of the same script
     produce identical SEMANTIC output (git shas excluded — they are inherently time-based).
  2. Halting proven: the loop halts after exactly 2 SUCCESSIVE passes with no NEW verified claim
     (SPEC 7.3, RS2 should_halt rebased onto epochs).
  3. Budget cap binds: a pass at/over $3 EST is blocked (pure gate boundary + loop behavior).
  4. Live-E2E is deferred to FE2E (mocked in-sprint) — asserted implicitly (no backend import/call).
Plus: the seed builder mirrors build_schedule_seed's byte shape; subtree scoping; the dag merge
wrapper; the observer-dispatch seam (F3 parse_observer_task -> run_observer); offline/deterministic scan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.forest import events as fe
from core.forest.store import create_program
from core.forest.sources import ForestSource
from core.research.metalane import MetaClaim, MetaLedger
from core.scheduler import build_schedule_seed

from core.forest.epoch import (
    DEFAULT_EPOCH_DATE,
    HALT_DRY_PASSES,
    PER_PASS_BUDGET_USD,
    EpochArmResult,
    EpochBudgetError,
    EpochLoopResult,
    EpochPass,
    build_epoch_seed,
    dispatch_observer_task,
    enforce_pass_budget,
    new_verified,
    pass_within_budget,
    run_epoch_loop,
    seed_subtree,
    should_halt,
    synthesize_merge,
)

EPOCH_SRC = (Path(__file__).resolve().parents[2] / "core" / "forest" / "epoch.py").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mc(subject: str, predicate: str = "implements", obj: str = "core/x.py:10", scope: str = "uplift"):
    """A deterministic MetaClaim (claim_id is a stable hash of its fields)."""
    return MetaClaim(subject=subject, predicate=predicate, object=obj, scope=scope)


class MockArm:
    """An injected research-arm mock (the mockable graph invoke). Returns scripted EpochArmResults in
    order; once the script is exhausted it returns an empty (dry) result. Records every seed it saw."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.seeds = []

    async def __call__(self, seed):
        self.seeds.append(seed)
        if self._i < len(self._script):
            res = self._script[self._i]
            self._i += 1
            return res
        return EpochArmResult(claims=[], verified=[], est_cost=0.0)


def _arm_result(claims_verified, est_cost=1.0, council_ran=False):
    """Build an EpochArmResult from a list of (MetaClaim, verified_bool)."""
    claims = [c for c, _ in claims_verified]
    verified = [bool(v) for _, v in claims_verified]
    return EpochArmResult(claims=claims, verified=verified, est_cost=est_cost, council_ran=council_ran)


def _semantic(result: EpochLoopResult, store) -> dict:
    """Extract the SHA-independent semantic output for a determinism comparison."""
    forest_events = [
        {k: e.get(k) for k in ("kind", "ts", "epoch_id", "trigger", "halted", "verified_claims",
                               "amount_usd", "label")}
        for e in store.events()
        if e.get("kind") in {"epoch_open", "epoch_close", "spend"}
    ]
    return {
        "halted": result.halted,
        "blocked": result.blocked,
        "total_est_cost": result.total_est_cost,
        "all_verified_ids": list(result.all_verified_ids),
        "passes": [
            {"index": p.index, "epoch_id": p.epoch_id, "trigger": p.trigger,
             "verified_ids": sorted(p.verified_ids), "new_verified_ids": sorted(p.new_verified_ids),
             "merged": p.merged, "halted": p.halted, "blocked": p.blocked,
             "council_ran": p.council_ran, "est_cost": p.est_cost}
            for p in result.passes
        ],
        "forest_events": forest_events,
        "claims": sorted(store.read()["claims"].keys()),
    }


def _run(store, arm, **kw):
    kw.setdefault("program_id", store.program_id)
    kw.setdefault("times", list(range(1000, 1000 + kw.get("max_passes", 8))))
    kw.setdefault("meta_ledger_path", str(store.repo.parent / f"{store.program_id}__meta.jsonl"))
    return asyncio.run(run_epoch_loop(store, arm_invoke=arm, **kw))


# ---------------------------------------------------------------------------
# Part A — seed + subtree scoping
# ---------------------------------------------------------------------------

def test_build_epoch_seed_mirrors_schedule_seed_shape():
    """build_epoch_seed's byte shape is a SUPERSET of build_schedule_seed (so a real arm routes it)."""
    base = build_schedule_seed({"name": "p"}, {"task": "t"}, "2026-07-08T00:00:00")
    seed = build_epoch_seed("prog", "abc123", epoch_id="e0", task="do it")
    # every key the scheduler seed carries is present (mirrors the AgentState shape).
    assert set(base).issubset(set(seed)), set(base) - set(seed)
    assert seed["conversation_id"] == "epoch:prog:e0"
    assert seed["task"] == "do it"
    assert seed["backend"] == "local"
    assert seed["user_id"] is None
    assert seed["pipes"] == ["research"]
    assert seed["program_id"] == "prog"
    assert seed["subtree_ref"] == "abc123"
    assert seed["epoch_id"] == "e0"


def test_build_epoch_seed_council_pipes():
    seed = build_epoch_seed("prog", "ref", epoch_id="e1", pipes=("research", "council"))
    assert seed["pipes"] == ["research", "council"]


def test_seed_subtree_whole_program(tmp_path):
    store = create_program("segprog", root=tmp_path)
    store.append_events([
        fe.measurement(node_id="H1", metric="uplift", value=1.0, source="s", ts=1),
        fe.measurement(node_id="H2", metric="uplift", value=2.0, source="s", ts=2),
    ])
    scoped = seed_subtree(store)  # None -> whole program
    kinds = [e["kind"] for e in scoped["events"]]
    assert kinds.count("measurement") == 2
    assert scoped["ref"] == store.head()


def test_seed_subtree_scopes_to_node_ids(tmp_path):
    store = create_program("segprog2", root=tmp_path)
    store.append_events([
        fe.measurement(node_id="H1", metric="uplift", value=1.0, source="s", ts=1),
        fe.measurement(node_id="H2", metric="uplift", value=2.0, source="s", ts=2),
        fe.spend(amount_usd=0.5, label="x", ts=3),  # program-wide (no node_id) -> always kept
    ])
    scoped = seed_subtree(store, node_ids={"H1"})
    node_ids = [e.get("node_id") for e in scoped["events"] if e["kind"] == "measurement"]
    assert node_ids == ["H1"]  # H2 excluded
    # the program-wide spend event (no node_id) is retained
    assert any(e["kind"] == "spend" for e in scoped["events"])


# ---------------------------------------------------------------------------
# Part B — budget gate (accept #3, pure)
# ---------------------------------------------------------------------------

def test_pass_within_budget_boundary():
    assert pass_within_budget(2.99) is True
    assert pass_within_budget(2.999) is True
    assert pass_within_budget(3.00) is False   # AT the cap -> blocked
    assert pass_within_budget(3.01) is False   # OVER the cap -> blocked
    assert PER_PASS_BUDGET_USD == 3.0


def test_enforce_pass_budget_raises_at_and_over_cap():
    enforce_pass_budget(2.99)          # under: no raise
    enforce_pass_budget(0.0)
    with pytest.raises(EpochBudgetError):
        enforce_pass_budget(3.00, epoch_id="e0")   # at the cap
    with pytest.raises(EpochBudgetError):
        enforce_pass_budget(4.5, epoch_id="e0")    # over the cap


# ---------------------------------------------------------------------------
# Part C — halt rule (accept #2, pure)
# ---------------------------------------------------------------------------

def test_should_halt_boundary():
    assert should_halt(0) is False
    assert should_halt(1) is False
    assert should_halt(2) is True    # 2 successive dry passes
    assert should_halt(3) is True
    assert HALT_DRY_PASSES == 2


def test_new_verified_excludes_seen():
    ledger = MetaLedger  # sanity that the symbol imports
    assert ledger is not None


def test_new_verified_computes_delta(tmp_path):
    meta = MetaLedger(str(tmp_path / "m.jsonl"))
    a, b = _mc("A"), _mc("B")
    meta.append(0, a, verified=True)
    meta.append(0, b, verified=True)
    assert new_verified(meta, 0, set()) == {a.claim_id, b.claim_id}
    assert new_verified(meta, 0, {a.claim_id}) == {b.claim_id}
    assert new_verified(meta, 0, {a.claim_id, b.claim_id}) == set()


# ---------------------------------------------------------------------------
# Part D — dag merge = synthesis wrapper
# ---------------------------------------------------------------------------

def test_synthesize_merge_lands_claims_and_merge_commit(tmp_path):
    store = create_program("merca", root=tmp_path)
    claims = [_mc("slotA", "implements", "core/a.py:1"), _mc("slotB", "depends_on", "core/b.py:2")]
    out = synthesize_merge(store.repo, date=DEFAULT_EPOCH_DATE, slug="epoch-e0",
                           verified_claims=claims)
    assert out["merged"] is True
    assert out["merge_sha"]
    assert set(out["surviving_keys"]) == {c.claim_id for c in claims}
    landed = store.read("HEAD")["claims"]
    assert set(landed.keys()) == {c.claim_id for c in claims}


def test_synthesize_merge_empty_is_a_no_merge_dry_pass(tmp_path):
    store = create_program("mercb", root=tmp_path)
    head0 = store.head()
    out = synthesize_merge(store.repo, date=DEFAULT_EPOCH_DATE, slug="epoch-e0", verified_claims=[])
    assert out["merged"] is False
    assert out["merge_sha"] is None
    assert out["final_ref"] == head0        # HEAD unchanged: nothing synthesized
    assert store.read("HEAD")["claims"] == {}


# ---------------------------------------------------------------------------
# Part E — end-to-end mocked epoch (accept #1)
# ---------------------------------------------------------------------------

def test_epoch_runs_end_to_end_mocked(tmp_path):
    store = create_program("e2e", root=tmp_path)
    a, b = _mc("slotA", "implements", "core/a.py:1"), _mc("slotB", "satisfies", "core/b.py:2")
    arm = MockArm([_arm_result([(a, True), (b, True)], est_cost=1.25)])  # then dry passes -> halt
    res = _run(store, arm, trigger="manual", max_passes=6)

    p0 = res.passes[0]
    # seed -> claims produced -> verified -> merge commit
    assert sorted(p0.verified_ids) == sorted([a.claim_id, b.claim_id])
    assert p0.merged is True and p0.merge_sha
    assert p0.est_cost == 1.25
    # verified claims appended as ledger events: the epoch_close event carries them + a merge landed them
    led = store.read()
    assert set(led["claims"].keys()) == {a.claim_id, b.claim_id}
    kinds = [e["kind"] for e in store.events()]
    assert "epoch_open" in kinds and "spend" in kinds and "epoch_close" in kinds
    close = [e for e in store.events() if e["kind"] == "epoch_close" and e["epoch_id"] == "e0"][0]
    assert sorted(close["verified_claims"]) == sorted([a.claim_id, b.claim_id])
    spend = [e for e in store.events() if e["kind"] == "spend"][0]
    assert spend["amount_usd"] == 1.25 and spend["est"] is True
    # loop eventually halts (2 dry passes after the productive one)
    assert res.halted is True
    assert res.all_verified_ids == sorted([a.claim_id, b.claim_id])
    # the mock arm was actually spawned with an epoch seed
    assert arm.seeds and arm.seeds[0]["conversation_id"] == "epoch:e2e:e0"
    assert arm.seeds[0]["pipes"] == ["research"]


def test_epoch_loop_is_deterministic_across_fresh_runs(tmp_path):
    def build(pid):
        store = create_program(pid, root=tmp_path)
        a, b = _mc("slotA", "implements", "core/a.py:1"), _mc("slotB", "satisfies", "core/b.py:2")
        arm = MockArm([_arm_result([(a, True), (b, True)], est_cost=1.25)])
        return store, arm

    s1, arm1 = build("det1")
    r1 = _run(s1, arm1, trigger="manual", max_passes=6)
    s2, arm2 = build("det2")
    r2 = _run(s2, arm2, trigger="manual", max_passes=6)
    # SHA-independent semantic output is identical (claims are content-addressed -> same ids).
    assert _semantic(r1, s1) == _semantic(r2, s2)


def test_council_synthesis_pass_flows_pipes_and_flag(tmp_path):
    store = create_program("counc", root=tmp_path)
    a = _mc("slotA", "implements", "core/a.py:1")

    seen_pipes = []

    class _CouncilArm(MockArm):
        async def __call__(self, seed):
            seen_pipes.append(seed["pipes"])
            return await super().__call__(seed)

    arm = _CouncilArm([_arm_result([(a, True)], est_cost=0.5, council_ran=True)])
    res = _run(store, arm, council=True, max_passes=4)
    assert seen_pipes[0] == ["research", "council"]
    assert res.passes[0].council_ran is True


# ---------------------------------------------------------------------------
# Part F — halting (accept #2)
# ---------------------------------------------------------------------------

def test_halts_after_exactly_two_dry_passes(tmp_path):
    """An arm that never produces a new verified claim -> halt after exactly 2 passes."""
    store = create_program("dry", root=tmp_path)
    arm = MockArm([])  # every pass is dry (empty result)
    res = _run(store, arm, max_passes=8)
    assert res.halted is True
    assert len(res.passes) == 2                 # exactly 2 dry passes then halt
    assert res.passes[-1].halted is True
    assert res.all_verified_ids == []


def test_productive_then_two_dry_passes_halts(tmp_path):
    """Productive pass 0, then dry passes 1 & 2 -> halt at pass 2 (3 passes total)."""
    store = create_program("prod", root=tmp_path)
    a = _mc("slotA", "implements", "core/a.py:1")
    arm = MockArm([_arm_result([(a, True)], est_cost=0.5)])  # pass 0 productive; rest dry
    res = _run(store, arm, max_passes=8)
    assert res.halted is True
    assert len(res.passes) == 3
    assert res.passes[0].new_verified_ids == [a.claim_id]
    assert res.passes[1].new_verified_ids == [] and res.passes[1].halted is False
    assert res.passes[2].new_verified_ids == [] and res.passes[2].halted is True


def test_re_verifying_same_claim_counts_as_dry(tmp_path):
    """A claim verified again in a later pass is NOT a NEW verified claim -> still a dry pass."""
    store = create_program("reverify", root=tmp_path)
    a = _mc("slotA", "implements", "core/a.py:1")
    arm = MockArm([
        _arm_result([(a, True)], est_cost=0.5),   # pass 0: A new
        _arm_result([(a, True)], est_cost=0.5),   # pass 1: A again -> dry (streak 1)
        _arm_result([(a, True)], est_cost=0.5),   # pass 2: A again -> dry (streak 2) -> halt
    ])
    res = _run(store, arm, max_passes=8)
    assert res.halted is True
    assert len(res.passes) == 3
    assert res.passes[1].new_verified_ids == []
    assert res.passes[2].halted is True


# ---------------------------------------------------------------------------
# Part G — budget cap binds in the loop (accept #3)
# ---------------------------------------------------------------------------

def test_loop_budget_cap_blocks_pass_and_does_not_invoke_arm(tmp_path):
    store = create_program("budg", root=tmp_path)
    a = _mc("slotA", "implements", "core/a.py:1")
    arm = MockArm([_arm_result([(a, True)], est_cost=0.1)])
    res = _run(store, arm, max_passes=4, cost_estimator=lambda seed: 3.0)  # AT the cap -> blocked
    assert res.blocked is True
    assert len(res.passes) == 1 and res.passes[0].blocked is True
    # the arm was NEVER spawned and NO epoch events were written
    assert arm.seeds == []
    kinds = [e["kind"] for e in store.events()]
    assert "epoch_open" not in kinds and "spend" not in kinds and "epoch_close" not in kinds


def test_loop_under_budget_runs_and_records_spend(tmp_path):
    store = create_program("underb", root=tmp_path)
    a = _mc("slotA", "implements", "core/a.py:1")
    arm = MockArm([_arm_result([(a, True)], est_cost=2.5)])
    res = _run(store, arm, max_passes=4, cost_estimator=lambda seed: 2.99)  # just under cap
    assert res.blocked is False
    assert res.passes[0].blocked is False
    spend = [e for e in store.events() if e["kind"] == "spend"][0]
    assert spend["amount_usd"] == 2.5


# ---------------------------------------------------------------------------
# Part H — observer dispatch seam (F3 -> F4 wiring)
# ---------------------------------------------------------------------------

def test_dispatch_observer_task_runs_observer(tmp_path):
    store = create_program("obs", root=tmp_path)
    source = ForestSource(id="uplift", kind="testpipe_uplift", node_id="H1", metric="uplift")
    task = f"forest.observe program={store.program_id} source=uplift"
    res = dispatch_observer_task(
        task,
        store_for=lambda pid: store,
        source_for=lambda pid, sid: source,
        payload_for=lambda pid, sid: [{"value": 1.0}, {"value": 2.0}],
        ts=42,
    )
    assert res is not None
    assert res.count == 2
    landed = [e for e in store.events() if e["kind"] == "measurement"]
    assert len(landed) == 2


def test_dispatch_observer_task_ignores_non_observer_task(tmp_path):
    out = dispatch_observer_task(
        "something else entirely",
        store_for=lambda pid: None,
        source_for=lambda pid, sid: None,
        payload_for=lambda pid, sid: None,
        ts=1,
    )
    assert out is None


# ---------------------------------------------------------------------------
# Part I — offline / deterministic (accept #4, inv. 13)
# ---------------------------------------------------------------------------

def test_epoch_module_is_offline_and_deterministic():
    banned = ["time.time(", "datetime.now(", "datetime.utcnow(", "random.", "uuid.",
              "import requests", "httpx", "aiohttp", "socket.", "urllib"]
    for tok in banned:
        assert tok not in EPOCH_SRC, f"epoch.py must not reference {tok!r} (offline/deterministic)"


def test_epoch_module_does_not_edit_graph_or_scheduler():
    # epoch.py rides existing arms: it never imports/edits the graph routing.
    assert "core.graph" not in EPOCH_SRC
    assert "create_graph" not in EPOCH_SRC
