"""bobpipe_cycle.py — the MS9-FE2E capstone: the bobpipe seed program, weeks-in-minutes.

This is the SHARED driver for the full research-forest cycle (SPEC-RESEARCH-FOREST §5, §2). It wires
the eight committed forest surfaces (F1-F8) + the W2 digest emitter into ONE deterministic pass and is
consumed by BOTH:

  * ``tests/forest/test_forest_e2e.py`` — the ``core/forest/`` integration test (hermetic: a FakeQdrant
    double + a deterministic fixture embedder), which asserts each stage's outcome; and
  * ``run_bobpipe_e2e.py`` — the runnable harness, which drives the SAME cycle against bobclaw's own
    Qdrant (:6353, a uniquely-named TEST collection dropped at teardown) and writes the real artifacts.

**Honest scope (mega-sprint invariant 13).** The cycle runs on **synthetic backfill + the F4 mocked
research arm** — it is deterministic and offline. The single sanctioned micro LIVE testpipe-uplift run
was SPENT by F6 (``sprints/RESULTS-F6.md``: 3 toy contracts, deepseek, pass@1=1.0, $0.000425, real
measurement/spend/experiment_run events in a real git ledger, sha ``eebcadb286cc``). FE2E cites those
as the "real live artifacts" and does NOT run another live fleet pass (inv. 13 — the carve-out is
spent). Everything here is proposal-only where actuation is gated (inv. 14).

All eight surfaces are imported READ-ONLY and composed; this module edits none of them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ── F1 substrate: program ledgers + registry ────────────────────────────────
from core.forest import events as fe
from core.forest.program import ForestRegistry
# ── F3 observers + trigger verbs ────────────────────────────────────────────
from core.forest.observe import DELTA_THRESHOLD, evaluate_delta, run_observer
from core.forest.sources import ForestSource
# ── F4 epoch loop (RS2 rebased) ─────────────────────────────────────────────
from core.forest.epoch import (
    PER_PASS_BUDGET_USD,
    EpochArmResult,
    enforce_pass_budget,
    run_epoch_loop,
)
from core.forest.epoch import EpochBudgetError
# ── F5 measurement-entailment ───────────────────────────────────────────────
from core.verify.measurement import entail_measurement
# ── F7 forks + observational A/B ────────────────────────────────────────────
from core.forest.fork import (
    ForkProposal,
    approve_fork,
    arm_from_bools,
    judge_ab_race,
    propose_fork,
)
# ── research primitives the epoch arm speaks ────────────────────────────────
from core.research.metalane import MetaClaim
# ── W2 digest emitter (MERGED) ──────────────────────────────────────────────
from core.watch.digest import AlertThreshold, WatchEvent, build_digest

# The seed program's standing question (SPEC §5).
STANDING_QUESTION = (
    "What do BoB-pipe slots contribute to uplift, and how does that move over time?"
)
DEFAULT_PROGRAM_ID = "bobpipe-uplift"

# Synthetic "weeks" of observation (weeks-in-minutes). Each is a measurement ``ts`` (a comparable
# ISO date string; entailment orders a series by ts).
WEEKS = [
    "2026-05-04", "2026-05-11", "2026-05-18",
    "2026-05-25", "2026-06-01", "2026-06-08",
]
# Epoch pass wall-clock dates (the F4 loop stamps epoch events with these).
EPOCH_TIMES = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"]

# ── The three backfilled hypothesis nodes + their weekly synthetic streams. ──
# H1 uplift TRENDS up over 6 weeks (a `trend` claim, PV);
# H2 pass@1 sits in a healthy band over 5 weeks (a `level` claim, PV);
# H3 latency has only 2 obs — BELOW the §7.2 floor (5) → default-FAIL → U.
H1_UPLIFT = [0.010, 0.028, 0.041, 0.063, 0.088, 0.121]        # 6 weeks, increasing
H2_PASS_AT_1 = [0.78, 0.82, 0.80, 0.88, 0.91]                 # 5 weeks, in [0.7, 1.0]
H3_LATENCY_MS = [124.0, 118.0]                                # 2 weeks (below floor)


@dataclass
class StageLog:
    """One stage's headline outcome (for the RESULTS narrative + on_stage callbacks)."""

    n: int
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CycleResult:
    """Everything the eight stages produced — asserted by the test, rendered by the harness."""

    program_id: str
    standing_question: str
    base_ref: str
    # stage 2
    n_backfilled: int = 0
    delta_count: int = 0
    delta_fired: bool = False
    delta_threshold: int = DELTA_THRESHOLD
    # stage 3
    epoch_passes: int = 0
    epoch_halted: bool = False
    epoch_blocked: bool = False
    epoch_verified_ids: list = field(default_factory=list)
    epoch_total_est_cost: float = 0.0
    budget_cap: float = PER_PASS_BUDGET_USD
    budget_cap_binds: bool = False
    # stage 4
    ab_judged: bool = False
    ab_winner: Optional[str] = None
    ab_merge_sha: Optional[str] = None
    ab_delta: float = 0.0
    # stage 5
    tags: dict = field(default_factory=dict)          # node_id -> tag string (PV/VS/U)
    entailments: dict = field(default_factory=dict)   # node_id -> MeasurementEntailment
    # stage 6
    fork_proposal: Optional[ForkProposal] = None
    fork_artifact_path: Optional[str] = None
    fork_auto_applied: bool = True                    # inv.14: must end FALSE
    # stage 7
    projection_count: int = 0
    fresh_after_rebuild: bool = False
    stale_after_advance: bool = False
    fresh_after_second_rebuild: bool = False
    retrieve_hits: int = 0
    # stage 8
    digest_markdown: str = ""
    digest_doc: dict = field(default_factory=dict)
    digest_id: str = ""
    stages: list = field(default_factory=list)

    def log(self, n: int, name: str, ok: bool, detail: str = "") -> None:
        self.stages.append(StageLog(n=n, name=name, ok=ok, detail=detail))


class _MockEpochArm:
    """The injected research arm (F4 ``arm_invoke``) — deterministic, NO live fleet call (inv. 13).

    Returns one scripted productive :class:`EpochArmResult` (two verified, corpus-anchored claims about
    which pipe slots carry uplift), then empty/dry results — so the loop halts after 2 dry passes
    (SPEC §7.3) exactly as F4 proved. Records every seed it was spawned with.
    """

    def __init__(self) -> None:
        self.seeds: list = []
        self._i = 0
        c1 = MetaClaim(subject="audit_slot", predicate="implements",
                       object="core/testpipe/pipeline.py:42", scope="uplift")
        c2 = MetaClaim(subject="repair_slot", predicate="implements",
                       object="core/testpipe/repair.py:17", scope="uplift")
        self._script = [EpochArmResult(claims=[c1, c2], verified=[True, True], est_cost=1.25,
                                       council_ran=True, detail="mock research+council synthesis")]
        self.claims = [c1, c2]

    async def __call__(self, seed: dict) -> EpochArmResult:
        self.seeds.append(seed)
        if self._i < len(self._script):
            res = self._script[self._i]
            self._i += 1
            return res
        return EpochArmResult(claims=[], verified=[], est_cost=0.0)


def _sources() -> dict:
    """The three fixture-driven forest measurement sources (F3)."""
    return {
        "H1": ForestSource(id="uplift-obs", kind="testpipe_uplift", node_id="H1",
                           metric="uplift", unit="fraction"),
        "H2": ForestSource(id="passat1-obs", kind="eval_results", node_id="H2",
                           metric="pass_at_1", unit="fraction"),
        "H3": ForestSource(id="latency-obs", kind="flight_spend", node_id="H3",
                           metric="latency_ms", unit="ms"),
    }


def _backfill_streams() -> list:
    """(source_key, week_ts, value) tuples for the synthetic multi-week history (stage 1/2)."""
    srcs = _sources()
    out = []
    for i, v in enumerate(H1_UPLIFT):
        out.append(("H1", WEEKS[i], v))
    for i, v in enumerate(H2_PASS_AT_1):
        out.append(("H2", WEEKS[i], v))
    for i, v in enumerate(H3_LATENCY_MS):
        out.append(("H3", WEEKS[i], v))
    return srcs, out


def build_epoch_digest(result: CycleResult) -> Any:
    """Render the epoch's digest by REUSING W2's ``core/watch/digest.py`` (SPEC pack §6 stage 8).

    Maps the cycle's headline outcomes onto ``WatchEvent``s and calls ``build_digest`` → a
    ``DigestArtifact`` carrying ``.to_markdown()`` (the markdown artifact) and ``.to_doc()`` (the
    content-addressed doc). A ``fork``-category threshold fires one alert (a fork proposal is a
    human-decision item, inv. 14).
    """
    events = [
        WatchEvent(
            id=f"epoch:{result.program_id}",
            title=f"Hypothesize epoch: {len(result.epoch_verified_ids)} verified claim(s), "
                  f"halted={result.epoch_halted}",
            category="epoch", severity="notable", source="forest.epoch",
            summary=f"delta-triggered ({result.delta_count} evidence events ≥ "
                    f"{result.delta_threshold}); est ${result.epoch_total_est_cost:.2f} ≤ "
                    f"${result.budget_cap:.0f} cap",
        ),
    ]
    for node_id in sorted(result.tags):
        tag = result.tags[node_id]
        ent = result.entailments.get(node_id)
        ck = getattr(ent, "claim_kind", "?")
        floor_met = getattr(ent, "floor_met", None)
        sev = "notable" if tag == "PV" else "info"
        events.append(WatchEvent(
            id=f"tag:{node_id}",
            title=f"{node_id} [{ck}] → {tag}",
            category="entailment", severity=sev, source="verify.measurement",
            summary=("default-FAIL: observation floor not met" if floor_met is False
                     else f"entailed from {getattr(ent, 'n_observations', '?')} observations"),
        ))
    if result.ab_judged:
        events.append(WatchEvent(
            id=f"ab:{result.program_id}",
            title=f"A/B race decided → winner {result.ab_winner}",
            category="ab-race", severity="notable", source="forest.fork",
            summary=f"2-parent merge=synthesis recorded (delta={result.ab_delta:.3f})",
        ))
    if result.fork_proposal is not None:
        events.append(WatchEvent(
            id=f"fork:{result.fork_proposal.fork_id}",
            title=f"Fork PROPOSED: {result.fork_proposal.child_program_id} (proposal-only)",
            category="fork", severity="alert", source="forest.fork",
            summary=result.fork_proposal.rationale,
        ))
    thresholds = [AlertThreshold(name="fork-proposals-need-a-human", category="fork",
                                 min_count=1, level="alert")]
    return build_digest(
        events,
        thresholds=thresholds,
        title="bobpipe uplift — epoch digest",
        profile=result.program_id,
        group_by="category",
    )


async def run_cycle(
    *,
    registry: ForestRegistry,
    proj: Any,                       # a constructed core.forest.projection.ForestProjection
    program_id: str = DEFAULT_PROGRAM_ID,
    artifact_dir: Any,               # where the fork proposal artifact is written
    meta_ledger_path: Any,           # RS2 MetaLedger jsonl path (F4)
    on_stage: Optional[Callable[[StageLog], None]] = None,
) -> CycleResult:
    """Drive the full 8-stage research-forest cycle, weeks-in-minutes. Returns a :class:`CycleResult`.

    Deterministic + offline. The projection stage runs against whatever Qdrant client ``proj`` wraps
    (a FakeQdrant in the test; the real :6353 in the harness).
    """
    def _emit(log: StageLog) -> None:
        if on_stage is not None:
            on_stage(log)

    # ── Stage 0 — create the bobpipe program (F1) ───────────────────────────
    registry.create_program(program_id, question=STANDING_QUESTION, exist_ok=False)
    store = registry.open_store(program_id)
    base_ref = store.head()                       # pin the pre-backfill ref for the delta gate
    result = CycleResult(program_id=program_id, standing_question=STANDING_QUESTION,
                         base_ref=base_ref)
    result.log(0, "create program (F1)", True, f"{program_id} @ {base_ref[:12]}")
    _emit(result.stages[-1])

    # ── Stage 1 + 2 — synthetic backfill via observers ticking (F3) ─────────
    srcs, stream = _backfill_streams()
    n_backfilled = 0
    for src_key, week_ts, value in stream:
        src = srcs[src_key]
        obs = run_observer(store, src, [{"value": value}], week_ts)
        n_backfilled += obs.count
    result.n_backfilled = n_backfilled
    result.log(1, "synthetic multi-week backfill via observers (F3)", n_backfilled == len(stream),
               f"{n_backfilled} measurement events across {len(WEEKS)} weeks")
    _emit(result.stages[-1])

    # delta gate fires at the §7.3 threshold (≥ 10 subtree evidence events)
    trig = evaluate_delta(store, base_ref, threshold=DELTA_THRESHOLD)
    result.delta_count = trig.count or 0
    result.delta_fired = trig.fired
    result.log(2, "delta gate fires (F3 §7.3 ≥10)", trig.fired,
               f"{trig.count} new evidence events ≥ {DELTA_THRESHOLD} → fired={trig.fired}")
    _emit(result.stages[-1])

    # ── Stage 3 — ONE hypothesize epoch (F4, injected MOCK arm — NOT live) ──
    arm = _MockEpochArm()
    epoch_res = await run_epoch_loop(
        store,
        arm_invoke=arm,
        program_id=program_id,
        times=EPOCH_TIMES,
        meta_ledger_path=str(meta_ledger_path),
        trigger="delta",
        council=True,
        max_passes=len(EPOCH_TIMES),
        date=EPOCH_TIMES[0],
    )
    result.epoch_passes = len(epoch_res.passes)
    result.epoch_halted = epoch_res.halted
    result.epoch_blocked = epoch_res.blocked
    result.epoch_verified_ids = list(epoch_res.all_verified_ids)
    result.epoch_total_est_cost = epoch_res.total_est_cost
    # the $3/pass cap binds (SPEC §7.3) — prove the gate boundary rejects an at-cap estimate.
    try:
        enforce_pass_budget(PER_PASS_BUDGET_USD, PER_PASS_BUDGET_USD, epoch_id="probe")
        result.budget_cap_binds = False
    except EpochBudgetError:
        result.budget_cap_binds = True
    ok3 = (not epoch_res.blocked and epoch_res.halted
           and len(epoch_res.all_verified_ids) == 2
           and epoch_res.total_est_cost < PER_PASS_BUDGET_USD
           and result.budget_cap_binds)
    result.log(3, "one hypothesize epoch, mocked arm (F4)", ok3,
               f"{len(epoch_res.passes)} passes, halted={epoch_res.halted}, "
               f"{len(epoch_res.all_verified_ids)} verified, est ${epoch_res.total_est_cost:.2f} "
               f"< ${PER_PASS_BUDGET_USD:.0f} cap; cap-binds={result.budget_cap_binds}")
    _emit(result.stages[-1])

    # ── Stage 4 — observational A/B race → 2-parent merge=synthesis (F7) ────
    arm_a = arm_from_bools("armX", "H_ab", [True, True, True, True, False, True], label="audit=on")
    arm_b = arm_from_bools("armY", "H_ab", [True, False, False, True, False, False], label="audit=off")
    ab = judge_ab_race(store.repo, arm_a, arm_b, node_id="H_ab",
                       date="20260619", slug="ab-audit-slot")
    result.ab_judged = ab["judged"]
    result.ab_winner = ab["winner"]
    result.ab_merge_sha = ab["merge_sha"]
    result.ab_delta = ab["delta"]
    result.log(4, "observational A/B race, 2-parent merge=synthesis (F7)",
               bool(ab["judged"] and ab["merged"] and ab["merge_sha"]),
               f"winner={ab['winner']} delta={ab['delta']:.3f} merge_sha={str(ab['merge_sha'])[:12]}")
    _emit(result.stages[-1])

    # ── Stage 5 — measurement-entailment tags land (F5), default-FAIL <floor ─
    events = store.read()["events"]
    ent_h1 = entail_measurement(claim_kind="trend",
                                decision_rule={"metric": "uplift", "direction": "up"},
                                events=events, node_id="H1")
    ent_h2 = entail_measurement(claim_kind="level",
                                decision_rule={"metric": "pass_at_1", "lo": 0.7, "hi": 1.0},
                                events=events, node_id="H2")
    ent_h3 = entail_measurement(claim_kind="level",
                                decision_rule={"metric": "latency_ms", "lo": 0.0, "hi": 200.0},
                                events=events, node_id="H3")
    for nid, ent in (("H1", ent_h1), ("H2", ent_h2), ("H3", ent_h3)):
        result.entailments[nid] = ent
        result.tags[nid] = ent.tag.value
    ok5 = (ent_h1.tag.value == "PV" and ent_h2.tag.value == "PV"
           and ent_h3.tag.value == "U" and ent_h3.floor_met is False)
    result.log(5, "measurement-entailment tags land (F5)", ok5,
               f"H1(trend)={ent_h1.tag.value} H2(level)={ent_h2.tag.value} "
               f"H3(below-floor)={ent_h3.tag.value} (default-FAIL)")
    _emit(result.stages[-1])

    # ── Stage 6 — fork PROPOSAL artifact, proposal-only (F7, inv. 14) ───────
    subtree_ref = store.head()
    proposal = propose_fork(
        parent_store=store,
        child_program_id="bobpipe-audit-slot-uplift",
        node_ids=["H1"],
        rationale=("the per-slot audit uplift question outgrew the seed tree: it now needs its own "
                   "falsifiers, observer cadence, and epoch budget and can no longer share H1's "
                   "single measurement stream"),
        ts="2026-06-20",
        question="How much of total uplift does the audit slot alone contribute, week over week?",
        subtree_ref=subtree_ref,
        artifact_dir=str(artifact_dir),
    )
    result.fork_proposal = proposal
    result.fork_artifact_path = str(artifact_dir) + "/" + proposal.artifact_filename
    # inv. 14: NOTHING auto-applied — no child registered, no fork_approved on the parent ledger.
    child_registered = registry.exists(proposal.child_program_id)
    parent_has_fork_approved = any(e.get("kind") == "fork_approved" for e in store.read()["events"])
    result.fork_auto_applied = bool(child_registered or parent_has_fork_approved)
    result.log(6, "fork proposal artifact, proposal-only (F7 inv.14)",
               (not result.fork_auto_applied),
               f"artifact={proposal.artifact_filename}; child_registered={child_registered}; "
               f"parent_fork_approved={parent_has_fork_approved}")
    _emit(result.stages[-1])

    # ── Stage 7 — projection rebuilt + freshness green (F8, TEST collection) ─
    r = proj.rebuild_from_store(store, tree_id=program_id)
    result.projection_count = r["count"]
    result.fresh_after_rebuild = (proj.is_stale(program_id, store.projection_key()) is False)
    hits = proj.retrieve(query_text="uplift measurement trend", tree_id=program_id, k=5)
    result.retrieve_hits = len(hits)
    # advance the ledger → staleness detected → rebuild → fresh again
    store.append_events([fe.measurement(node_id="H1", metric="uplift", value=0.14,
                                        source="uplift-obs", ts="2026-06-15")])
    result.stale_after_advance = (proj.is_stale(program_id, store.projection_key()) is True)
    proj.rebuild_from_store(store, tree_id=program_id)
    result.fresh_after_second_rebuild = (proj.is_stale(program_id, store.projection_key()) is False)
    ok7 = (r["count"] > 0 and result.fresh_after_rebuild and result.stale_after_advance
           and result.fresh_after_second_rebuild and result.retrieve_hits > 0)
    result.log(7, "projection rebuilt + freshness green (F8)", ok7,
               f"{r['count']} points; fresh→advance→stale→rebuild→fresh; {len(hits)} retrieve hits")
    _emit(result.stages[-1])

    # ── Stage 8 — epoch digest artifact (reuse W2 digest.py) ────────────────
    digest = build_epoch_digest(result)
    result.digest_markdown = digest.to_markdown()
    result.digest_doc = digest.to_doc()
    result.digest_id = digest.digest_id
    result.log(8, "epoch digest artifact (W2 reuse)", bool(result.digest_id and result.digest_markdown),
               f"digest_id={result.digest_id[:16]}…, {len(digest.alerts)} alert(s)")
    _emit(result.stages[-1])

    return result


def all_stages_ok(result: CycleResult) -> bool:
    """True iff every stage reported ok."""
    return all(s.ok for s in result.stages) and len(result.stages) == 9
