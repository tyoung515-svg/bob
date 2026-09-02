"""fork.py — forest FORKS + observational A/B branches (MS9-F7).

A fork is a **child program seeded from a `read_ledger_at` projection of a subtree** of a parent
program. It is **proposal-only** (SPEC §1 / mega-sprint invariant 14): ``propose_fork`` emits a
proposal artifact + a gateway ``forest_fork`` approval item and NOTHING is actuated — no child
program, no registry edge, no parent-ledger write. Only the explicit (test-)approval path
``approve_fork`` actuates: it creates the child via F1 ``ForestRegistry.create_program(parent=...)``
(recording the BIDIRECTIONAL provenance edge), seeds the child ledger from the pinned subtree
projection, and records an explicit ``pull_from_parent`` event (later knowledge transfer is always
an explicit pull, never a live sync).

The fork proposal carries the mandatory **"why it can't stay a subtree" rationale** (SPEC §1). The
child's seed is **verifiable**: ``projection_seed_key`` is a content-addressed key over the subtree
projection, recorded on the child ledger so an auditor can recompute it from the parent at the
pinned ``subtree_ref`` and assert equality.

Observational **A/B branches** are sibling branches over the same measurement stream whose Beta
posteriors race; when the posteriors separate (or a deadline epoch hits) ``judge_ab_race`` records a
council judgment as a **2-parent merge=synthesis** on the git-DAG — the R6 primitive, composed via
``core/research/dag.py`` (never re-implemented).

Fork **proposers** are SEAMS only (SPEC §8): ``CuratorForkProposer`` ("question outgrew its tree")
and ``NegspaceForkProposer`` ("question nobody asked", RS1). Each consumes an *injected* signal
source — there is NO live negspace/curator run in this module. Fixture tests drive the seams.

Determinism (mega-sprint invariant 13): NO clock, NO uuid, NO random, NO network. Every timestamp is
a caller-supplied argument; every id is a content-addressed hash. All ledger/registry primitives are
imported READ-ONLY from F1/F4 and composed — this module writes only via those public APIs (a
program's own git ledger under the gitignored data dir) plus an optional proposal artifact file.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from core.forest.events import (
    ForestError,
    fork_approved as _ev_fork_approved,
    fork_proposed as _ev_fork_proposed,
    pull_from_parent as _ev_pull_from_parent,
)
from core.forest.hypothesis import Beta, posterior_mean
from core.forest.store import ProgramStore, validate_program_id
from core.research.dag import ResearchDag, RoundInputs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APPROVAL_KIND = "forest_fork"
SEED_KEY_PREFIX = "forkseed:sha256:"
# Posteriors "separate" when their Beta means differ by at least this much (SPEC §1 observational
# A/B; the exact epsilon is a forest-tuning default, overridable per call).
AB_SEPARATION_THRESHOLD = 0.2
DEFAULT_AB_DATE = "20260101"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ForkError(ForestError):
    """Raised on invalid fork proposal/approval or A/B judgment misuse."""
    pass


# ---------------------------------------------------------------------------
# Subtree projection (PURE) — the verifiable fork seed
# ---------------------------------------------------------------------------
def _event_in_subtree(event: dict, node_set: set) -> bool:
    """True iff *event* is scoped to a node in *node_set* (by its ``node_id``).

    Program-wide events (no ``node_id`` — epoch/spend/fork markers) are NOT part of a subtree seed:
    a fork carries the subtree's *node-scoped* content forward, not the parent's program envelope.
    """
    nid = event.get("node_id")
    return nid is not None and nid in node_set


def _claim_in_subtree(claim_id: str, claim: dict, node_set: set) -> bool:
    """True iff *claim* belongs to the subtree — either it carries a ``node_id`` in *node_set*, or
    the claim's own id is one of the subtree node ids (a node keyed by its node id)."""
    return claim.get("node_id") in node_set or claim_id in node_set


def subtree_projection(ledger: dict, node_ids: Sequence[str]) -> dict:
    """PURE: filter a ledger dict (as returned by ``ProgramStore.read``) to a subtree.

    Returns ``{"node_ids": <sorted>, "events": [...], "claims": {...}}`` — the events/claims scoped
    to *node_ids*, in ledger order (events) / sorted-by-id (claims) so the shape is deterministic.
    """
    node_set = set(node_ids)
    if not node_set:
        raise ForkError("subtree_projection requires a non-empty node_ids set")
    events = [e for e in ledger.get("events", []) if _event_in_subtree(e, node_set)]
    claims_in = ledger.get("claims", {}) or {}
    claims = {
        cid: claims_in[cid]
        for cid in sorted(claims_in)
        if _claim_in_subtree(cid, claims_in[cid], node_set)
    }
    return {"node_ids": sorted(node_set), "events": events, "claims": claims}


def projection_seed_key(projection: dict) -> str:
    """PURE, content-addressed key over a subtree projection — the VERIFIABLE fork seed key.

    Two byte-identical subtree projections yield the same key, independent of parent commit sha, so
    the child's recorded seed key can be re-derived from the parent at the pinned ``subtree_ref``.
    """
    payload = json.dumps(projection, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{SEED_KEY_PREFIX}{digest}"


def read_subtree_projection(
    store: ProgramStore, node_ids: Sequence[str], ref: str = "HEAD"
) -> tuple[dict, str]:
    """READ-ONLY: read the parent ledger at *ref* and return ``(projection, seed_key)`` for a subtree.

    Uses only ``ProgramStore.read`` (which reads git blobs — no working-tree write). ``ref`` should
    be pinned to a concrete sha by the caller so the seed is stable between proposal and approval.
    """
    ledger = store.read(ref)
    proj = subtree_projection(ledger, node_ids)
    return proj, projection_seed_key(proj)


# ---------------------------------------------------------------------------
# Fork proposal artifact (proposal-only — inv. 14)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ForkProposal:
    """A proposal to fork a subtree of *parent_program* into a new child program.

    Proposal-only: constructing/holding one actuates NOTHING. It surfaces as a gateway
    ``forest_fork`` approval item; the child is created only by :func:`approve_fork`.
    """

    fork_id: str
    parent_program: str
    child_program_id: str
    subtree_ref: str
    node_ids: tuple
    rationale: str            # the mandatory "why it can't stay a subtree" field (SPEC §1)
    seed_key: str             # verifiable projection seed key over the subtree at subtree_ref
    ts: Any
    question: Optional[str] = None
    proposer: str = "manual"

    @property
    def artifact_filename(self) -> str:
        """The filesystem-safe artifact filename this proposal writes under an artifact dir."""
        return f"{_safe_artifact_stem(self.fork_id)}.json"

    def to_dict(self) -> dict:
        """A JSON-serializable dict of the proposal (the artifact body)."""
        return {
            "fork_id": self.fork_id,
            "parent_program": self.parent_program,
            "child_program_id": self.child_program_id,
            "subtree_ref": self.subtree_ref,
            "node_ids": list(self.node_ids),
            "rationale": self.rationale,
            "seed_key": self.seed_key,
            "question": self.question,
            "proposer": self.proposer,
            "ts": self.ts,
        }

    def approval_item(self) -> dict:
        """The gateway approval item: ``action_type='forest_fork'`` + details carrying the rationale.

        Matches the gateway approvals surface (``action_type`` + ``details`` JSONB). Proposal-only —
        it is surfaced for a human decision; nothing is applied until the decision is 'approve'.
        """
        return {
            "action_type": APPROVAL_KIND,
            "proposal_only": True,
            "details": {
                "fork_id": self.fork_id,
                "parent_program": self.parent_program,
                "child_program_id": self.child_program_id,
                "subtree_ref": self.subtree_ref,
                "node_ids": list(self.node_ids),
                # The "why it can't stay a subtree" rationale surfaces verbatim in the approvals pane.
                "rationale": self.rationale,
                "seed_key": self.seed_key,
                "question": self.question,
                "proposer": self.proposer,
            },
        }

    def fork_proposed_event(self) -> dict:
        """The ``fork_proposed`` ledger event object for this proposal (NOT appended by propose)."""
        return _ev_fork_proposed(
            fork_id=self.fork_id,
            parent_program=self.parent_program,
            rationale=self.rationale,
            ts=self.ts,
            subtree_ref=self.subtree_ref,
            child_program=self.child_program_id,
            node_ids=list(self.node_ids),
            seed_key=self.seed_key,
        )


def _derive_fork_id(parent_program: str, child_program_id: str, seed_key: str) -> str:
    """Deterministic, content-addressed fork id (no uuid/clock)."""
    raw = f"{parent_program}\x1f{child_program_id}\x1f{seed_key}"
    return "fork:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_UNSAFE_STEM = re.compile(r"[^A-Za-z0-9._-]")


def _safe_artifact_stem(fork_id: str) -> str:
    """A filesystem-safe stem for the proposal artifact filename.

    A caller-supplied ``fork_id`` (or the default ``fork:<hex>``) must NOT be able to escape the
    artifact dir or smuggle a path separator / ``..`` / a ``:`` — on Windows a raw ``fork:x`` would
    create an NTFS alternate data stream, not a file. Fold anything outside ``[A-Za-z0-9._-]`` to
    ``-``, strip leading/trailing separators, cap length, and never return empty.
    """
    stem = _UNSAFE_STEM.sub("-", fork_id).strip("-.")
    return (stem or "fork")[:64]


def propose_fork(
    *,
    parent_store: ProgramStore,
    child_program_id: str,
    node_ids: Sequence[str],
    rationale: str,
    ts: Any,
    question: Optional[str] = None,
    proposer: str = "manual",
    subtree_ref: str = "HEAD",
    fork_id: Optional[str] = None,
    artifact_dir: Optional[Any] = None,
) -> ForkProposal:
    """Build a fork PROPOSAL (proposal-only — actuates nothing).

    Reads the parent subtree (read-only) to compute the verifiable ``seed_key``. If *artifact_dir*
    is given, writes ONE proposal artifact ``<fork_id>.json`` there — the ONLY filesystem write this
    function performs. It does NOT create the child program, does NOT touch the registry, and does
    NOT append to the parent ledger (inv. 14: nothing auto-applies / auto-registers).

    Raises ``ForkError`` if the *rationale* ("why it can't stay a subtree") is missing/blank, if
    *node_ids* is empty, or if *child_program_id* is not a safe program id.
    """
    if not isinstance(rationale, str) or not rationale.strip():
        raise ForkError(
            "a fork proposal requires a non-empty 'why it can't stay a subtree' rationale (SPEC §1)"
        )
    node_ids = list(node_ids or [])
    if not node_ids:
        raise ForkError("propose_fork requires a non-empty node_ids subtree")
    validate_program_id(child_program_id)  # reuse F1's id guard (no traversal / separators)

    _proj, seed_key = read_subtree_projection(parent_store, node_ids, subtree_ref)
    parent_program = parent_store.program_id
    fid = fork_id or _derive_fork_id(parent_program, child_program_id, seed_key)

    proposal = ForkProposal(
        fork_id=fid,
        parent_program=parent_program,
        child_program_id=child_program_id,
        subtree_ref=subtree_ref,
        node_ids=tuple(node_ids),
        rationale=rationale.strip(),
        seed_key=seed_key,
        ts=ts,
        question=question,
        proposer=proposer,
    )

    if artifact_dir is not None:
        adir = pathlib.Path(artifact_dir)
        adir.mkdir(parents=True, exist_ok=True)
        artifact_path = adir / proposal.artifact_filename
        artifact_path.write_text(
            json.dumps(proposal.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    return proposal


# ---------------------------------------------------------------------------
# Fork approval (the EXPLICIT actuation path)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ForkApprovalResult:
    """Outcome of an approved fork: the seeded child + the provenance/seed events recorded."""

    fork_id: str
    parent_program: str
    child_program: str
    seed_key: str
    child_head: str
    child_event_ids: tuple
    parent_event_ids: tuple
    seed_verified: bool


def approve_fork(
    proposal: ForkProposal,
    registry: Any,
    *,
    ts: Any,
    notes: Optional[str] = None,
) -> ForkApprovalResult:
    """Actuate an APPROVED fork (the explicit human/test decision path).

    Steps:
      1. (fail-closed, ALWAYS) Re-derive the subtree ``seed_key`` from the parent at the pinned
         ``proposal.subtree_ref``; if it no longer matches the proposal's seed, ABORT — the subtree
         changed or the proposal was tampered. There is no bypass.
      2. Create the child via ``registry.create_program(child_id, parent=parent, ...)`` — F1 records
         the BIDIRECTIONAL provenance edge (child.parent + parent.children).
      3. Seed the CHILD ledger from the subtree PROJECTION: the projected node-scoped measurement
         events are materialized into the child (the one-time seed snapshot — NOT a live sync), plus a
         ``fork_approved`` event (carrying the verifiable ``seed_key``) and an explicit
         ``pull_from_parent`` event (from the pinned subtree_ref) — the later-transfer verb. (Claims
         are intentionally NOT synced — the child re-hypothesizes; SPEC §1 "no live sync".)
      4. Record provenance on the PARENT ledger too (a ``fork_approved`` marker) so both sides carry
         it at the ledger-truth level, not only in the registry catalog.

    The *registry* must already have the parent registered (F1 ``create_program(parent=...)`` rejects
    an unregistered parent). Returns a :class:`ForkApprovalResult`.
    """
    # 1) verifiable seed (fail-closed, always — no bypass)
    parent_store = registry.open_store(proposal.parent_program)
    proj, recomputed = read_subtree_projection(
        parent_store, proposal.node_ids, proposal.subtree_ref
    )
    if recomputed != proposal.seed_key:
        raise ForkError(
            f"fork {proposal.fork_id}: subtree seed key mismatch at {proposal.subtree_ref!r} "
            f"(proposal {proposal.seed_key}, recomputed {recomputed}) — refusing to seed a "
            f"child from a changed/tampered subtree"
        )

    # 2) child + bidirectional provenance edge (F1)
    registry.create_program(
        proposal.child_program_id,
        question=proposal.question,
        parent=proposal.parent_program,
        metadata={
            "fork_id": proposal.fork_id,
            "seed_key": proposal.seed_key,
            "subtree_ref": proposal.subtree_ref,
            "node_ids": list(proposal.node_ids),
            "proposer": proposal.proposer,
        },
    )
    child_store = registry.open_store(proposal.child_program_id)

    # 3) seed the child ledger from the projection + the explicit pull_from_parent transfer event
    seeded_events = list(proj["events"])  # the node-scoped measurement stream, snapshotted at the ref
    child_seed_ev = _ev_fork_approved(
        fork_id=proposal.fork_id,
        child_program=proposal.child_program_id,
        parent_program=proposal.parent_program,
        ts=ts,
        seed_key=proposal.seed_key,
        subtree_ref=proposal.subtree_ref,
        node_ids=list(proposal.node_ids),
        seeded_event_count=len(seeded_events),
    )
    child_pull_ev = _ev_pull_from_parent(
        parent_program=proposal.parent_program,
        from_ref=proposal.subtree_ref,
        ts=ts,
        notes=notes or f"seeded from fork {proposal.fork_id}",
        seed_key=proposal.seed_key,
        fork_id=proposal.fork_id,
    )
    child_head = child_store.append_events(
        [*seeded_events, child_seed_ev, child_pull_ev],
        message=f"forest: seed child {proposal.child_program_id} from fork {proposal.fork_id}",
    )

    # 4) provenance marker on the PARENT ledger (both-sides at ledger-truth level)
    parent_marker = _ev_fork_approved(
        fork_id=proposal.fork_id,
        child_program=proposal.child_program_id,
        parent_program=proposal.parent_program,
        ts=ts,
        seed_key=proposal.seed_key,
        subtree_ref=proposal.subtree_ref,
    )
    parent_store.append_events(
        [parent_marker],
        message=f"forest: fork {proposal.fork_id} spawned child {proposal.child_program_id}",
    )

    return ForkApprovalResult(
        fork_id=proposal.fork_id,
        parent_program=proposal.parent_program,
        child_program=proposal.child_program_id,
        seed_key=proposal.seed_key,
        child_head=child_head,
        child_event_ids=(child_seed_ev["id"], child_pull_ev["id"]),
        parent_event_ids=(parent_marker["id"],),
        seed_verified=True,  # reached only after the fail-closed seed check passed
    )


# ---------------------------------------------------------------------------
# Observational A/B branches — Beta posteriors race → 2-parent merge=synthesis
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ABArm:
    """One A/B arm: a Beta posterior accumulated over a measurement stream for a hypothesis node."""

    arm_id: str
    node_id: str
    beta: Beta
    label: str = ""
    n_obs: int = 0

    @property
    def mean(self) -> float:
        return posterior_mean(self.beta)


def arm_from_bools(
    arm_id: str,
    node_id: str,
    observations: Sequence[bool],
    *,
    label: str = "",
    weight: float = 1.0,
    prior: Optional[Beta] = None,
) -> ABArm:
    """Fold a stream of boolean observations into an :class:`ABArm` (PURE).

    Mirrors ``hypothesis.apply_evidence``: a supporting obs → ``alpha += weight``; a contradicting
    obs → ``beta += weight``. Starts from a Beta(1,1) prior unless *prior* is given.
    """
    b = Beta(alpha=prior.alpha, beta=prior.beta) if prior is not None else Beta()
    n = 0
    for obs in observations:
        if obs:
            b.alpha += weight
        else:
            b.beta += weight
        n += 1
    return ABArm(arm_id=arm_id, node_id=node_id, beta=b, label=label, n_obs=n)


def posteriors_separated(
    arm_a: ABArm, arm_b: ABArm, *, threshold: float = AB_SEPARATION_THRESHOLD
) -> bool:
    """True iff the two arms' Beta posterior means differ by at least *threshold*."""
    return abs(arm_a.mean - arm_b.mean) >= threshold


def ab_winner(arm_a: ABArm, arm_b: ABArm) -> ABArm:
    """The arm with the higher posterior mean (ties broken deterministically by arm_id)."""
    if arm_a.mean > arm_b.mean:
        return arm_a
    if arm_b.mean > arm_a.mean:
        return arm_b
    return arm_a if arm_a.arm_id <= arm_b.arm_id else arm_b


def _ab_claim_id(node_id: str, arm_a: ABArm, arm_b: ABArm) -> str:
    """Deterministic bid_key for an A/B comparison judgment."""
    raw = f"ab\x1f{node_id}\x1f{arm_a.arm_id}\x1f{arm_b.arm_id}"
    return "ab:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def judge_ab_race(
    repo: Any,
    arm_a: ABArm,
    arm_b: ABArm,
    *,
    node_id: Optional[str] = None,
    date: str = DEFAULT_AB_DATE,
    slug: str = "ab-race",
    deadline_reached: bool = False,
    separation_threshold: float = AB_SEPARATION_THRESHOLD,
    asserter: str = "ab-council",
    base_branch: Optional[str] = None,
    ledger_dir: str = "ledger",
) -> dict:
    """Judge an observational A/B race and, if decided, record a **2-parent merge=synthesis**.

    The judgment fires when the posteriors SEPARATE (means differ ≥ *separation_threshold*) OR a
    *deadline_reached* epoch hit. When it fires, the winning arm's ``comparison`` claim is synthesized
    into the report via ``core/research/dag.py`` — the ``merge_synthesis`` (``--no-ff``) commit is the
    2-parent merge (base tip + arm branch tip). When it does NOT fire, nothing is written.

    Returns a dict: ``{judged, reason, winner, loser, delta, claim_id, merge_sha, merged, final_ref,
    surviving_keys}``.
    """
    nid = node_id or arm_a.node_id
    separated = posteriors_separated(arm_a, arm_b, threshold=separation_threshold)
    if not (separated or deadline_reached):
        return {
            "judged": False,
            "reason": "posteriors not separated and no deadline epoch",
            "winner": None,
            "loser": None,
            "delta": abs(arm_a.mean - arm_b.mean),
            "claim_id": None,
            "merge_sha": None,
            "merged": False,
            "final_ref": None,
            "surviving_keys": [],
        }

    winner = ab_winner(arm_a, arm_b)
    loser = arm_b if winner is arm_a else arm_a
    delta = winner.mean - loser.mean
    claim_id = _ab_claim_id(nid, arm_a, arm_b)
    reason = "posteriors separated" if separated else "deadline epoch"

    claim = {
        "subject": nid,
        "predicate": "ab_winner",
        "numeric_value": None,
        "cited_source_id": None,
        "text": (
            f"A/B[{nid}] winner={winner.arm_id} (mean={winner.mean:.4f}) over "
            f"{loser.arm_id} (mean={loser.mean:.4f}); delta={delta:.4f}; reason={reason}"
        ),
        "bid_key": claim_id,
    }
    ri = RoundInputs(
        claims=(claim,),
        surviving_keys=(claim_id,),
        could_not_verify=(),
        asserter_backend=asserter,
    )
    dag = ResearchDag(repo, date=date, slug=slug, base_branch=base_branch, ledger_dir=ledger_dir)
    res = dag.run([ri])
    last = res.rounds[-1] if res.rounds else None

    return {
        "judged": True,
        "reason": reason,
        "winner": winner.arm_id,
        "loser": loser.arm_id,
        "delta": delta,
        "claim_id": claim_id,
        "merge_sha": last.merge_sha if last else None,
        "merged": bool(last.merged) if last else False,
        "final_ref": res.final_ref,
        "surviving_keys": list(res.surviving_keys),
    }


# ---------------------------------------------------------------------------
# Fork proposer SEAMS (interfaces only — NO live runs, SPEC §8)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ForkSignal:
    """A candidate fork surfaced by a proposer seam (fixture-injected — never a live run).

    Carries everything :func:`propose_fork` needs: the subtree ``node_ids``, the proposed child id +
    ``question``, and the mandatory ``rationale`` ("why it can't stay a subtree").
    """

    child_program_id: str
    node_ids: tuple
    rationale: str
    question: Optional[str] = None
    subtree_ref: str = "HEAD"


class ForkProposer:
    """Base fork-proposer SEAM.

    A proposer maps *injected* signals into fork PROPOSALS (proposal-only). It NEVER runs a live
    detector: ``signal_source`` is a caller-supplied zero-arg callable returning ``ForkSignal``\\s
    (a fixture in tests). Concrete seams only stamp their ``proposer_name`` + rationale framing.
    """

    proposer_name = "base"
    rationale_prefix = ""

    def __init__(self, signal_source: Callable[[], Sequence[ForkSignal]]) -> None:
        self._signal_source = signal_source

    def _frame_rationale(self, rationale: str) -> str:
        if self.rationale_prefix and not rationale.startswith(self.rationale_prefix):
            return f"{self.rationale_prefix}: {rationale}"
        return rationale

    def scan(
        self, *, parent_store: ProgramStore, ts: Any, artifact_dir: Optional[Any] = None
    ) -> list[ForkProposal]:
        """Consume injected signals and emit fork proposals — actuates NOTHING (proposal-only)."""
        proposals: list[ForkProposal] = []
        for sig in self._signal_source() or []:
            proposals.append(
                propose_fork(
                    parent_store=parent_store,
                    child_program_id=sig.child_program_id,
                    node_ids=sig.node_ids,
                    rationale=self._frame_rationale(sig.rationale),
                    ts=ts,
                    question=sig.question,
                    proposer=self.proposer_name,
                    subtree_ref=sig.subtree_ref,
                    artifact_dir=artifact_dir,
                )
            )
        return proposals


class CuratorForkProposer(ForkProposer):
    """Seam: the Curator proposes a fork when a question **outgrew its tree** (SPEC §1)."""

    proposer_name = "curator"
    rationale_prefix = "question outgrew its tree"


class NegspaceForkProposer(ForkProposer):
    """Seam: RS1 negspace proposes a fork for a **question nobody asked** (SPEC §1).

    Per SPEC §8, this is a SEAM only — there is NO live negspace run here; the ``signal_source`` is
    injected (a fixture in tests). This class must never import or trigger ``core.research.negspace``.
    """

    proposer_name = "negspace"
    rationale_prefix = "question nobody asked"


__all__ = [
    "APPROVAL_KIND",
    "SEED_KEY_PREFIX",
    "AB_SEPARATION_THRESHOLD",
    "DEFAULT_AB_DATE",
    "ForkError",
    # subtree projection / seed
    "subtree_projection",
    "projection_seed_key",
    "read_subtree_projection",
    # proposal + approval
    "ForkProposal",
    "ForkApprovalResult",
    "propose_fork",
    "approve_fork",
    # A/B
    "ABArm",
    "arm_from_bools",
    "posteriors_separated",
    "ab_winner",
    "judge_ab_race",
    # proposer seams
    "ForkSignal",
    "ForkProposer",
    "CuratorForkProposer",
    "NegspaceForkProposer",
]
