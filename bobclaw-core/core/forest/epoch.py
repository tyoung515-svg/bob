"""
core/forest/epoch.py — forest HYPOTHESIZE EPOCH loop (SPEC section 2)
and RS2 MetaLedger generation loop rebased onto wall-clock epochs.

PURPOSE:
    One epoch = one pass: read the ledger at HEAD scoped to the subtree,
    spawn the research arm (injected, no graph edits), optional council
    synthesis, verified claims append as ledger events, merge via
    core/research/dag.py.  Halting after 2 successive passes with no
    new verified claim.  Per-pass budget cap $3 EST enforced.

DETERMINISM / OFFLINE (HARD, mega-sprint invariant 13):
    No clock, no random, no uuid, no network.  Every timestamp is a
    caller-supplied argument.  The research arm is an injected async
    callable (mockable).  Standard library + exact imports only.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

from core.forest.events import ForestError, epoch_open, epoch_close, spend
from core.forest.observe import parse_observer_task, run_observer, ObserveResult
from core.research.metalane import MetaClaim, MetaLedger
from core.research.dag import ResearchDag, RoundInputs

logger = logging.getLogger("bobclaw.forest.epoch")

__all__ = [
    "EpochError",
    "EpochBudgetError",
    "PER_PASS_BUDGET_USD",
    "HALT_DRY_PASSES",
    "DEFAULT_EPOCH_DATE",
    "build_epoch_seed",
    "seed_subtree",
    "pass_within_budget",
    "enforce_pass_budget",
    "new_verified",
    "should_halt",
    "synthesize_merge",
    "EpochArmResult",
    "EpochPass",
    "EpochLoopResult",
    "run_epoch_loop",
    "dispatch_observer_task",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class EpochError(ForestError):
    pass

class EpochBudgetError(EpochError):
    pass

# ---------------------------------------------------------------------------
# Constants (SPEC 7.3 ratified)
# ---------------------------------------------------------------------------

PER_PASS_BUDGET_USD: float = 3.0
HALT_DRY_PASSES: int = 2
DEFAULT_EPOCH_DATE: str = "2026-07-08"

# ---------------------------------------------------------------------------
# PART A — Seed (mirrors core.scheduler.build_schedule_seed byte shape)
# ---------------------------------------------------------------------------

def build_epoch_seed(
    program_id: str,
    subtree_ref: str,
    *,
    epoch_id: str,
    task: str = "",
    pipes=("research",),
    face_hint: str = "researcher",
    subtree: Optional[dict] = None,
) -> dict:
    """Return a seed dict shaped like build_schedule_seed's AgentState seed,
    plus forest epoch fields."""
    return {
        "messages": [],
        "task": task,
        "conversation_id": f"epoch:{program_id}:{epoch_id}",
        "user_id": None,
        "face_id": face_hint,
        "model_override": None,
        "backend_override": None,
        "team": None,
        "profile_name": None,
        "backend": "local",
        "tools_allowed": [],
        "approval_required": False,
        "approval_response": None,
        "artifacts": [],
        "error": None,
        "project_instructions": None,
        "pipes": list(pipes),
        "program_id": program_id,
        "subtree_ref": subtree_ref,
        "subtree": subtree,
        "epoch_id": epoch_id,
    }


def seed_subtree(
    store: Any,
    node_ids: Optional[set] = None,
    ref: str = "HEAD",
) -> dict:
    """Seed step: read the program ledger at *ref* and scope to *node_ids*.

    Returns dict with keys "ref", "events", "claims".
    Pure read-only operation; no writes.
    """
    led = store.read(ref)
    if node_ids is None:
        # Return a shallow copy of the relevant fields
        return {
            "ref": led["ref"],
            "events": list(led["events"]),
            "claims": dict(led["claims"]),
        }
    node_ids_set = set(node_ids)
    filtered_events = []
    for e in led["events"]:
        node_id = e.get("node_id")
        if node_id is None:
            # program-wide events like epoch_open/close/spend
            filtered_events.append(e)
        elif node_id in node_ids_set:
            filtered_events.append(e)
    return {
        "ref": led["ref"],
        "events": filtered_events,
        "claims": dict(led["claims"]),
    }

# ---------------------------------------------------------------------------
# PART B — Budget gate (SPEC 7.3)
# ---------------------------------------------------------------------------

def pass_within_budget(est_cost: float, cap: float = PER_PASS_BUDGET_USD) -> bool:
    """Return True if *est_cost* is strictly below *cap*."""
    return float(est_cost) < float(cap)


def enforce_pass_budget(
    est_cost: float,
    cap: float = PER_PASS_BUDGET_USD,
    *,
    epoch_id: str = "",
) -> None:
    """Raise EpochBudgetError if *est_cost* is at or above *cap*."""
    if not pass_within_budget(est_cost, cap):
        raise EpochBudgetError(
            f"epoch {epoch_id}: pass EST ${float(est_cost):.2f} at/over "
            f"the per-pass cap ${float(cap):.2f} — blocked"
        )

# ---------------------------------------------------------------------------
# PART C — Halt rule (SPEC 7.3)
# ---------------------------------------------------------------------------

def new_verified(
    meta_ledger: MetaLedger,
    generation: int,
    seen_ids: set,
) -> set:
    """Return the set of claim ids that are verified in the given *generation*
    and were NOT in *seen_ids*."""
    vc = meta_ledger.verified_claims(generation)  # list of dicts with "claim_id"
    return {r["claim_id"] for r in vc} - set(seen_ids)


def should_halt(dry_pass_streak: int, threshold: int = HALT_DRY_PASSES) -> bool:
    """Return True if *dry_pass_streak* >= *threshold*."""
    return int(dry_pass_streak) >= int(threshold)

# ---------------------------------------------------------------------------
# PART D — DAG merge = synthesis wrapper
# ---------------------------------------------------------------------------

def _claim_to_dag_dict(mc: MetaClaim) -> dict:
    """Map a MetaClaim to the dag claim dict shape (bid_key == mc.claim_id)."""
    return {
        "subject": mc.subject,
        "predicate": mc.predicate,
        "numeric_value": None,
        "cited_source_id": None,
        "text": f"{mc.subject} {mc.predicate} {mc.object}",
        "bid_key": mc.claim_id,
    }


def synthesize_merge(
    repo: Any,                      # pathlib.Path to the program git repo
    *,
    date: str,
    slug: str,
    verified_claims: list,
    could_not_verify: tuple = (),
    asserter: str = "research-arm",
    base_branch: Optional[str] = None,
    ledger_dir: str = "ledger",
) -> dict:
    """Merge = synthesis of one epoch's claims onto the program git‑DAG."""
    surviving = [_claim_to_dag_dict(mc) for mc in verified_claims]
    surviving_keys = tuple(c["bid_key"] for c in surviving)
    cnv_claims = [
        {
            "subject": e.get("subject", ""),
            "predicate": e.get("predicate", ""),
            "numeric_value": None,
            "cited_source_id": None,
            "text": e.get("text", ""),
            "bid_key": e["bid_key"],
        }
        for e in could_not_verify
        if e.get("bid_key") and e["bid_key"] not in surviving_keys
    ]
    ri = RoundInputs(
        claims=tuple(surviving + cnv_claims),
        surviving_keys=surviving_keys,
        could_not_verify=tuple(could_not_verify),
        asserter_backend=asserter,
    )
    dag = ResearchDag(
        repo,
        date=date,
        slug=slug,
        base_branch=base_branch,
        ledger_dir=ledger_dir,
    )
    res = dag.run([ri])
    last = res.rounds[-1] if res.rounds else None
    return {
        "final_ref": res.final_ref,
        "merge_sha": last.merge_sha if last else None,
        "merged": bool(last.merged) if last else False,
        "surviving_keys": list(res.surviving_keys),
    }

# ---------------------------------------------------------------------------
# PART E — Result carriers
# ---------------------------------------------------------------------------

@dataclass
class EpochArmResult:
    """What the injected research arm returns (the mockable graph invoke)."""
    claims: list = field(default_factory=list)        # list[MetaClaim]
    verified: list = field(default_factory=list)      # parallel list[bool]
    est_cost: float = 0.0                              # actual EST spend (dollars)
    council_ran: bool = False                          # did optional council run?
    detail: str = ""

    def verified_claims(self):
        """Return list of MetaClaim where the parallel flag is True."""
        return [c for c, ok in zip(self.claims, self.verified) if ok]


@dataclass
class EpochPass:
    """One pass within an epoch loop."""
    index: int
    epoch_id: str
    trigger: str
    seed_ref: str
    claim_ids: list = field(default_factory=list)
    verified_ids: list = field(default_factory=list)
    new_verified_ids: list = field(default_factory=list)
    est_cost: float = 0.0
    merge_sha: Optional[str] = None
    merged: bool = False
    council_ran: bool = False
    halted: bool = False
    blocked: bool = False
    open_sha: Optional[str] = None
    close_sha: Optional[str] = None

    def as_dict(self):
        return {
            k: getattr(self, k)
            for k in (
                "index",
                "epoch_id",
                "trigger",
                "seed_ref",
                "claim_ids",
                "verified_ids",
                "new_verified_ids",
                "est_cost",
                "merge_sha",
                "merged",
                "council_ran",
                "halted",
                "blocked",
                "open_sha",
                "close_sha",
            )
        }


@dataclass
class EpochLoopResult:
    """Result of running an epoch loop (one or more passes)."""
    passes: tuple = ()                 # tuple[EpochPass]
    halted: bool = False
    blocked: bool = False
    total_est_cost: float = 0.0
    all_verified_ids: list = field(default_factory=list)
    final_ref: Optional[str] = None

    def as_dict(self):
        return {
            "passes": [p.as_dict() for p in self.passes],
            "halted": self.halted,
            "blocked": self.blocked,
            "total_est_cost": self.total_est_cost,
            "all_verified_ids": list(self.all_verified_ids),
            "final_ref": self.final_ref,
        }

# ---------------------------------------------------------------------------
# PART F — The epoch loop
# ---------------------------------------------------------------------------

async def run_epoch_loop(
    store: Any,
    *,
    arm_invoke: Callable[[dict], Awaitable[EpochArmResult]],
    program_id: str,
    times: Sequence,
    meta_ledger_path: Any,
    subtree_node_ids: Optional[set] = None,
    council: bool = False,
    trigger: str = "manual",
    max_passes: int = 8,
    date: str = DEFAULT_EPOCH_DATE,
    budget_cap: float = PER_PASS_BUDGET_USD,
    halt_threshold: int = HALT_DRY_PASSES,
    cost_estimator: Optional[Callable[[dict], float]] = None,
) -> EpochLoopResult:
    """Drive up to *max_passes* epochs.

    Dependencies are injected (fully offline/deterministic).
    """
    meta = MetaLedger(meta_ledger_path)
    est_fn = cost_estimator if cost_estimator is not None else (lambda seed: 0.0)
    passes: list = []
    seen_verified: set = set()
    dry_streak: int = 0
    total_cost: float = 0.0
    halted: bool = False
    blocked: bool = False
    final_ref = store.head()

    for i in range(max_passes):
        if i >= len(times):
            raise EpochError(
                f"run_epoch_loop: times has {len(times)} entries but pass {i} was reached"
            )
        ts = times[i]
        epoch_id = f"e{i}"
        subtree_ref = store.head()
        scoped = seed_subtree(store, subtree_node_ids, ref=subtree_ref)
        pipes = ("research", "council") if council else ("research",)
        task = f"forest.epoch program={program_id} epoch={epoch_id}"
        seed = build_epoch_seed(
            program_id,
            subtree_ref,
            epoch_id=epoch_id,
            task=task,
            pipes=pipes,
            subtree=scoped,
        )

        # Budget gate
        est = est_fn(seed)
        try:
            enforce_pass_budget(est, budget_cap, epoch_id=epoch_id)
        except EpochBudgetError:
            logger.warning(
                "epoch %s blocked by per-pass budget cap (est=%s cap=%s)",
                epoch_id,
                est,
                budget_cap,
            )
            passes.append(
                EpochPass(
                    index=i,
                    epoch_id=epoch_id,
                    trigger=trigger,
                    seed_ref=subtree_ref,
                    est_cost=float(est),
                    blocked=True,
                )
            )
            blocked = True
            break

        # Open the epoch (ledger event)
        open_sha = store.append_events(
            [
                epoch_open(
                    epoch_id=epoch_id, trigger=trigger, ts=ts, base_commit=subtree_ref
                )
            ],
            message=f"forest: epoch {epoch_id} open ({trigger})",
        )

        # Spawn the research arm (the mockable graph invoke)
        arm: EpochArmResult = await arm_invoke(seed)

        # Spend (EST-badged spend event)
        store.append_events(
            [
                spend(
                    amount_usd=float(arm.est_cost),
                    label=f"epoch {epoch_id} research arm",
                    ts=ts,
                    est=True,
                )
            ],
            message=f"forest: epoch {epoch_id} spend",
        )
        total_cost += float(arm.est_cost)

        # Record verified claims into the RS2 MetaLedger (generation == pass index)
        verified_mcs: list = []
        for mc, ok in zip(arm.claims, arm.verified):
            meta.append(i, mc, verified=bool(ok))
            if ok:
                verified_mcs.append(mc)

        verified_ids = [mc.claim_id for mc in verified_mcs]
        newv = new_verified(meta, i, seen_verified)

        # Merge = synthesis via core/research/dag.py
        merge = synthesize_merge(
            store.repo,
            date=date,
            slug=f"epoch-{epoch_id}",
            verified_claims=verified_mcs,
            asserter="research-arm",
        )

        # Halt bookkeeping (SPEC 7.3)
        if newv:
            dry_streak = 0
            seen_verified |= newv
        else:
            dry_streak += 1
        halted = should_halt(dry_streak, halt_threshold)

        # Close the epoch
        close_sha = store.append_events(
            [
                epoch_close(
                    epoch_id=epoch_id,
                    ts=ts,
                    halted=halted,
                    verified_claims=verified_ids,
                )
            ],
            message=f"forest: epoch {epoch_id} close",
        )

        final_ref = merge.get("final_ref") or store.head()
        passes.append(
            EpochPass(
                index=i,
                epoch_id=epoch_id,
                trigger=trigger,
                seed_ref=subtree_ref,
                claim_ids=[mc.claim_id for mc in arm.claims],
                verified_ids=verified_ids,
                new_verified_ids=sorted(newv),
                est_cost=float(arm.est_cost),
                merge_sha=merge.get("merge_sha"),
                merged=merge.get("merged", False),
                council_ran=bool(arm.council_ran),
                halted=halted,
                open_sha=open_sha,
                close_sha=close_sha,
            )
        )

        if halted:
            logger.info("epoch loop halted after pass %s (dry_streak=%s)", i, dry_streak)
            break

    return EpochLoopResult(
        passes=tuple(passes),
        halted=halted,
        blocked=blocked,
        total_est_cost=total_cost,
        all_verified_ids=sorted(seen_verified),
        final_ref=final_ref,
    )

# ---------------------------------------------------------------------------
# PART G — Observer dispatch seam (wire F3's parse_observer_task -> run_observer)
# ---------------------------------------------------------------------------

def dispatch_observer_task(
    task: str,
    *,
    store_for: Callable[[str], Any],
    source_for: Callable[[str, str], Any],
    payload_for: Callable[[str, str], Any],
    ts: Any,
) -> Optional[ObserveResult]:
    """Dispatch a forest observer task using injected resolvers.

    Returns an ObserveResult if the task was parsed successfully, else None.
    """
    parsed = parse_observer_task(task)
    if not parsed or not parsed.get("program") or not parsed.get("source"):
        return None
    program = parsed["program"]
    source_id = parsed["source"]
    store = store_for(program)
    source = source_for(program, source_id)
    payload = payload_for(program, source_id)
    return run_observer(store, source, payload, ts)
