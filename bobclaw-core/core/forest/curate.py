"""curate.py — the Research Forest CURATOR seam (MS9-F9).

SPEC-RESEARCH-FOREST §2 "Curate" (the slow loop), three responsibilities:

  * **Dormancy sweep** (§7.2): advance one curator epoch over a tree's hypothesis nodes; a node with
    ``DORMANT_EPOCHS`` (=3) consecutive zero-evidence epochs — and not already supported/refuted —
    transitions to ``dormant``.
  * **Archive proposal**: when a tree is ALL dormant/resolved (every node dormant, supported, or
    refuted) → a **final-synthesis + stop-observers** proposal artifact. The program repo is KEPT
    (never deleted); the proposal only recommends stopping the observers.
  * **Crystallize-out proposal**: SUPPORTED findings promoted toward the wiki LKS as a doc bundle —
    **proposal artifacts ONLY**. A human merges; the live corpora (the wiki LKS, any Qdrant
    collection) are **NEVER** written by the fleet.

Everything here is **proposal-only** (mega-sprint invariant 14) and **PURE + deterministic**
(invariant 13): NO clock, NO uuid, NO random, NO network. Every timestamp is a caller-supplied
argument; every id is a content-addressed hash. The ONLY filesystem writes any function performs are
proposal-artifact file(s) under an explicitly-passed ``artifact_dir`` — and only when that argument is
given. With ``artifact_dir=None`` nothing at all is written; the proposal dataclasses are returned for
a human/approval path to act on. NOTHING auto-applies, and nothing is ever written to a live corpus.

Read-only imports of F2 (``core.forest.hypothesis`` status machine + §7.2 constants), the F1
``core.forest.events`` ``archive_proposed`` constructor, and the F1 ``validate_program_id`` id guard.
This module writes NO ledger, NO registry, NO index — it classifies in-memory hypothesis nodes and
emits proposal artifacts.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from core.forest.events import ForestError, archive_proposed as _ev_archive_proposed
from core.forest.hypothesis import (
    HypothesisNode,
    STATUS_DORMANT,
    STATUS_REFUTED,
    STATUS_SUPPORTED,
    advance_epoch,
    posterior_mean,
)
from core.forest.store import validate_program_id

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: Gateway approval kinds these proposals surface under (additive; SPEC §6 always_human gates
#: "tree archive" and "crystallize-out merge"). Proposal-only — the dict is a *shape* for the
#: approvals pane, never an actuation.
ARCHIVE_APPROVAL_KIND = "forest_archive"
CRYSTALLIZE_APPROVAL_KIND = "forest_crystallize"

#: The crystallize-out human-merge destination — a NAME only. It is recorded in the proposal so the
#: approvals pane can say where a human would merge; it is NEVER used as a write path (inv. 14).
DEFAULT_CRYSTALLIZE_TARGET = "wiki-lks"

#: A "resolved" node has reached a terminal verdict.
RESOLVED_STATUSES = frozenset({STATUS_SUPPORTED, STATUS_REFUTED})
#: A tree is archivable when EVERY node is inactive — dormant OR resolved (no node still open).
INACTIVE_STATUSES = frozenset({STATUS_DORMANT, STATUS_SUPPORTED, STATUS_REFUTED})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class CurateError(ForestError):
    """Raised on invalid curator input (empty tree, not-ready archive, nothing to crystallize)."""
    pass


# ---------------------------------------------------------------------------
# Artifact helpers (the ONLY filesystem writes — always under artifact_dir)
# ---------------------------------------------------------------------------
_UNSAFE_STEM = re.compile(r"[^A-Za-z0-9._-]")


def _safe_stem(name: str) -> str:
    """A filesystem-safe stem: fold anything outside ``[A-Za-z0-9._-]`` to ``-``, strip leading/
    trailing separators, cap length, never return empty. (A raw ``id:`` colon on Windows would make
    an NTFS alternate data stream rather than a file — this prevents that.)"""
    stem = _UNSAFE_STEM.sub("-", str(name)).strip("-.")
    return (stem or "artifact")[:64]


def _write_json_artifact(artifact_dir: Any, filename: str, body: dict) -> pathlib.Path:
    """Write ONE JSON proposal artifact under *artifact_dir* (created if missing) and return its path.

    This is the ONLY place curate.py touches the filesystem, and it is reached only when a caller
    passes an explicit ``artifact_dir``. It never writes anywhere else — no live corpus, no ledger.
    """
    adir = pathlib.Path(artifact_dir)
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / filename
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _write_text_artifact(bundle_dir: Any, filename: str, text: str) -> pathlib.Path:
    """Write one text doc under *bundle_dir* (a subdir of artifact_dir) and return its path."""
    bdir = pathlib.Path(bundle_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    path = bdir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _content_id(kind: str, program_id: str, parts: Sequence[str]) -> str:
    """A deterministic, content-addressed id (no uuid/clock) over its defining parts."""
    raw = "\x1f".join([kind, program_id, *parts])
    return f"{kind}:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Small node accessors (tolerant of HypothesisNode or a duck-typed stand-in)
# ---------------------------------------------------------------------------
def _node_id(node: Any) -> str:
    return getattr(node, "id")


def _node_status(node: Any) -> str:
    return getattr(node, "status")


def supported_nodes(nodes: Sequence[Any]) -> list:
    """The nodes whose status is ``supported`` (the crystallize-out candidates)."""
    return [n for n in nodes if _node_status(n) == STATUS_SUPPORTED]


def is_tree_dormant_or_resolved(nodes: Sequence[Any]) -> bool:
    """True iff the tree is non-empty and EVERY node is dormant or resolved (none still open)."""
    nodes = list(nodes)
    return bool(nodes) and all(_node_status(n) in INACTIVE_STATUSES for n in nodes)


#: Readable alias for the archive-readiness predicate.
ready_to_archive = is_tree_dormant_or_resolved


# ---------------------------------------------------------------------------
# Dormancy sweep (§7.2)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DormancyTransition:
    """One node's status change across a single curator epoch."""

    node_id: str
    from_status: str
    to_status: str
    epochs_without_evidence: int
    new_evidence: int

    @property
    def changed(self) -> bool:
        return self.from_status != self.to_status

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "epochs_without_evidence": self.epochs_without_evidence,
            "new_evidence": self.new_evidence,
        }


@dataclass(frozen=True)
class SweepResult:
    """The outcome of one dormancy sweep over a tree."""

    transitions: tuple  # DormancyTransition per node, in input order
    newly_dormant: tuple  # node_ids that transitioned INTO dormant on this sweep
    all_inactive: bool  # every node is now dormant/resolved (archive-ready)

    @property
    def changed(self) -> tuple:
        """The transitions whose status actually changed on this sweep."""
        return tuple(t for t in self.transitions if t.changed)

    def to_dict(self) -> dict:
        return {
            "transitions": [t.to_dict() for t in self.transitions],
            "newly_dormant": list(self.newly_dormant),
            "all_inactive": self.all_inactive,
        }


def sweep_dormancy(
    nodes: Sequence[HypothesisNode],
    new_evidence: Optional[Mapping[str, int]] = None,
) -> SweepResult:
    """Advance ONE curator epoch over *nodes*, applying the §7.2 dormancy rule.

    For each node, look up its new-evidence count for this epoch in *new_evidence* (a
    ``{node_id: count}`` mapping; missing ⇒ 0), then call ``hypothesis.advance_epoch`` — which
    increments ``epochs_without_evidence`` on a zero-evidence epoch (resets to 0 otherwise) and
    refreshes the node status. A node reaching ``DORMANT_EPOCHS`` (=3) consecutive zero-evidence
    epochs — and NOT already supported/refuted (those verdicts take precedence over dormancy) —
    becomes ``dormant``.

    MUTATES each node in place (matching ``hypothesis.advance_epoch``, the F2 convention) and returns
    a :class:`SweepResult` recording every transition + which nodes went newly dormant.
    """
    nodes = list(nodes)
    ev = dict(new_evidence or {})
    transitions: list[DormancyTransition] = []
    newly_dormant: list[str] = []
    for node in nodes:
        nid = _node_id(node)
        before = _node_status(node)
        count = int(ev.get(nid, 0) or 0)
        advance_epoch(node, count)
        after = _node_status(node)
        transitions.append(
            DormancyTransition(
                node_id=nid,
                from_status=before,
                to_status=after,
                epochs_without_evidence=getattr(node, "epochs_without_evidence", 0),
                new_evidence=count,
            )
        )
        if after == STATUS_DORMANT and before != STATUS_DORMANT:
            newly_dormant.append(nid)
    all_inactive = bool(nodes) and all(_node_status(n) in INACTIVE_STATUSES for n in nodes)
    return SweepResult(
        transitions=tuple(transitions),
        newly_dormant=tuple(newly_dormant),
        all_inactive=all_inactive,
    )


# ---------------------------------------------------------------------------
# Archive proposal (proposal-only — inv. 14; repo is KEPT)
# ---------------------------------------------------------------------------
def _node_summary(node: Any) -> dict:
    """A compact, JSON-safe summary of a node for the archive/synthesis body."""
    return {
        "node_id": _node_id(node),
        "status": _node_status(node),
        "question": getattr(node, "question", "") or None,
        "hypothesis": getattr(node, "hypothesis", "") or None,
        "posterior_mean": round(posterior_mean(node), 6),
        "observations": getattr(node, "observations", 0),
    }


def _stop_observers(nodes: Sequence[Any]) -> tuple:
    """The (node_id, measurement_source) observers the archive proposal recommends stopping.

    Sorted + de-duplicated so the shape is deterministic. Nodes with no measurement_source are
    still listed (their observer, if any, keyed by node id) so nothing is silently left running.
    """
    seen = set()
    out = []
    for node in nodes:
        nid = _node_id(node)
        src = getattr(node, "measurement_source", None)
        key = (nid, src)
        if key in seen:
            continue
        seen.add(key)
        out.append({"node_id": nid, "measurement_source": src})
    out.sort(key=lambda d: (d["node_id"], d["measurement_source"] or ""))
    return tuple(out)


def _final_synthesis(program_id: str, summaries: Sequence[dict]) -> str:
    """A deterministic final-synthesis text for an archived tree (no clock/random)."""
    supported = [s for s in summaries if s["status"] == STATUS_SUPPORTED]
    refuted = [s for s in summaries if s["status"] == STATUS_REFUTED]
    dormant = [s for s in summaries if s["status"] == STATUS_DORMANT]
    lines = [
        f"Final synthesis for program '{program_id}': {len(summaries)} node(s) "
        f"({len(supported)} supported, {len(refuted)} refuted, {len(dormant)} dormant).",
    ]
    if supported:
        lines.append("Supported findings:")
        for s in supported:
            lines.append(
                f"  - {s['node_id']}: {s['question'] or s['hypothesis'] or '(no text)'} "
                f"[posterior={s['posterior_mean']}, n={s['observations']}]"
            )
    if refuted:
        lines.append("Refuted:")
        for s in refuted:
            lines.append(f"  - {s['node_id']}: {s['question'] or s['hypothesis'] or '(no text)'}")
    if dormant:
        lines.append("Dormant (no new evidence):")
        for s in dormant:
            lines.append(f"  - {s['node_id']}: {s['question'] or s['hypothesis'] or '(no text)'}")
    return "\n".join(lines)


@dataclass(frozen=True)
class ArchiveProposal:
    """A proposal to archive a fully dormant/resolved tree (proposal-only — actuates NOTHING).

    Holding one archives nothing and deletes nothing: the program repo is KEPT. It surfaces as a
    gateway ``forest_archive`` approval item; a human decides. The ``archive_proposed`` ledger event
    is available via :meth:`archive_proposed_event` but is NOT appended by the proposer.
    """

    program_id: str
    rationale: str
    archive_id: str
    node_summaries: tuple
    final_synthesis: str
    stop_observers: tuple
    ts: Any

    @property
    def artifact_filename(self) -> str:
        return f"archive-{_safe_stem(self.archive_id)}.json"

    def to_dict(self) -> dict:
        return {
            "kind": "archive_proposal",
            "proposal_only": True,
            "program_id": self.program_id,
            "archive_id": self.archive_id,
            "rationale": self.rationale,
            "node_summaries": [dict(s) for s in self.node_summaries],
            "final_synthesis": self.final_synthesis,
            "stop_observers": [dict(o) for o in self.stop_observers],
            "repo_kept": True,
            "ts": self.ts,
        }

    def approval_item(self) -> dict:
        """The gateway ``forest_archive`` approval item (``action_type`` + ``details`` JSONB).

        Mirrors the fork/experiment approval shape. Proposal-only: surfaced for a human decision;
        NOTHING is applied until the decision is 'approve'.
        """
        return {
            "action_type": ARCHIVE_APPROVAL_KIND,
            "proposal_only": True,
            "details": {
                "program_id": self.program_id,
                "archive_id": self.archive_id,
                "rationale": self.rationale,
                "final_synthesis": self.final_synthesis,
                "stop_observers": [dict(o) for o in self.stop_observers],
                "repo_kept": True,
            },
        }

    def archive_proposed_event(self) -> dict:
        """The ``archive_proposed`` ledger event for this proposal (NOT appended by the proposer)."""
        return _ev_archive_proposed(
            program_id=self.program_id,
            rationale=self.rationale,
            ts=self.ts,
            archive_id=self.archive_id,
            stop_observers=[dict(o) for o in self.stop_observers],
        )


def propose_archive(
    nodes: Sequence[HypothesisNode],
    *,
    program_id: str,
    ts: Any,
    rationale: Optional[str] = None,
    artifact_dir: Optional[Any] = None,
    require_ready: bool = True,
) -> ArchiveProposal:
    """Build an archive PROPOSAL for a fully dormant/resolved tree (proposal-only — actuates NOTHING).

    Fails loud (``CurateError``) on an empty tree, or — when *require_ready* (the default) — when any
    node is still active (open). The repo is KEPT: this never deletes a ledger. If *artifact_dir* is
    given, writes ONE proposal artifact ``archive-<id>.json`` there — the ONLY filesystem write. It
    does NOT append the ``archive_proposed`` event to any ledger (inv. 14: nothing auto-applies).
    """
    nodes = list(nodes)
    if not nodes:
        raise CurateError("propose_archive requires a non-empty node set")
    validate_program_id(program_id)  # reuse F1's path-safe id guard
    if require_ready and not is_tree_dormant_or_resolved(nodes):
        active = [_node_id(n) for n in nodes if _node_status(n) not in INACTIVE_STATUSES]
        raise CurateError(
            f"tree '{program_id}' is not all dormant/resolved — still-active nodes: {active}"
        )

    summaries = tuple(_node_summary(n) for n in nodes)
    stop = _stop_observers(nodes)
    synthesis = _final_synthesis(program_id, summaries)
    if rationale is None or not str(rationale).strip():
        n_dormant = sum(1 for s in summaries if s["status"] == STATUS_DORMANT)
        n_resolved = sum(1 for s in summaries if s["status"] in RESOLVED_STATUSES)
        rationale = (
            f"tree all dormant/resolved ({n_resolved} resolved, {n_dormant} dormant); "
            f"archive proposal — final synthesis + stop observers (repo kept)"
        )
    rationale = str(rationale).strip()

    archive_id = _content_id(
        "archive",
        program_id,
        [f"{s['node_id']}={s['status']}" for s in summaries],
    )

    proposal = ArchiveProposal(
        program_id=program_id,
        rationale=rationale,
        archive_id=archive_id,
        node_summaries=summaries,
        final_synthesis=synthesis,
        stop_observers=stop,
        ts=ts,
    )

    if artifact_dir is not None:
        _write_json_artifact(artifact_dir, proposal.artifact_filename, proposal.to_dict())

    return proposal


# ---------------------------------------------------------------------------
# Crystallize-out proposal (proposal-only — inv. 14; live corpora NEVER written)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CrystallizeDoc:
    """One doc in a crystallize-out bundle — a supported finding shaped for the wiki LKS."""

    slug: str
    title: str
    body: str
    node_id: str

    def to_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "body": self.body, "node_id": self.node_id}


@dataclass(frozen=True)
class CrystallizeProposal:
    """A proposal to crystallize supported findings toward the wiki LKS (proposal-only — inv. 14).

    The doc bundle TARGETS the wiki LKS but is emitted as proposal artifacts ONLY; ``target`` is a
    NAME, never a write path. A human merges — the fleet NEVER writes a live corpus.
    """

    program_id: str
    lks_namespace: str
    target: str
    crystallize_id: str
    docs: tuple
    ts: Any

    @property
    def artifact_filename(self) -> str:
        return f"crystallize-{_safe_stem(self.crystallize_id)}.json"

    @property
    def bundle_dirname(self) -> str:
        return _safe_stem(self.crystallize_id)

    def to_dict(self) -> dict:
        return {
            "kind": "crystallize_proposal",
            "proposal_only": True,
            "program_id": self.program_id,
            "lks_namespace": self.lks_namespace,
            "target": self.target,
            "crystallize_id": self.crystallize_id,
            "docs": [d.to_dict() for d in self.docs],
            "ts": self.ts,
        }

    def approval_item(self) -> dict:
        """The gateway ``forest_crystallize`` approval item — proposal-only (a human merges)."""
        return {
            "action_type": CRYSTALLIZE_APPROVAL_KIND,
            "proposal_only": True,
            "details": {
                "program_id": self.program_id,
                "lks_namespace": self.lks_namespace,
                "target": self.target,
                "crystallize_id": self.crystallize_id,
                "doc_count": len(self.docs),
                "doc_titles": [d.title for d in self.docs],
            },
        }


def _crystallize_doc(node: Any) -> CrystallizeDoc:
    """Render one supported node into a crystallize-out doc (deterministic)."""
    nid = _node_id(node)
    question = getattr(node, "question", "") or ""
    hypothesis = getattr(node, "hypothesis", "") or ""
    claim_kind = getattr(node, "claim_kind", "") or ""
    decision_rule = getattr(node, "decision_rule", None)
    evidence_refs = list(getattr(node, "evidence_refs", []) or [])
    pm = round(posterior_mean(node), 6)
    obs = getattr(node, "observations", 0)
    title = question or hypothesis or nid
    body_lines = [
        f"# {title}",
        "",
        f"- node_id: {nid}",
        f"- claim_kind: {claim_kind}" if claim_kind else "- claim_kind: (unset)",
        f"- hypothesis: {hypothesis}" if hypothesis else "- hypothesis: (unset)",
        f"- posterior_mean: {pm}",
        f"- observations: {obs}",
        f"- decision_rule: {decision_rule}" if decision_rule else "- decision_rule: (unset)",
        f"- evidence_refs: {evidence_refs}",
        "",
        "Status: SUPPORTED (Beta posterior ≥ threshold with sufficient observations, SPEC §7.2).",
        "PROPOSAL ONLY — a human reviews and merges this into the wiki LKS; nothing auto-applies.",
    ]
    return CrystallizeDoc(slug=_safe_stem(nid), title=title, body="\n".join(body_lines), node_id=nid)


def propose_crystallize(
    nodes: Sequence[HypothesisNode],
    *,
    program_id: str,
    ts: Any,
    lks_namespace: str = "research_forest",
    target: str = DEFAULT_CRYSTALLIZE_TARGET,
    artifact_dir: Optional[Any] = None,
) -> CrystallizeProposal:
    """Build a crystallize-out PROPOSAL from the SUPPORTED nodes in *nodes* (proposal-only — inv. 14).

    Fails loud (``CurateError``) when there is no supported node (nothing to crystallize). If
    *artifact_dir* is given, writes the proposal JSON plus the doc bundle (one ``.md`` per finding)
    UNDER *artifact_dir* — the ONLY filesystem writes. It NEVER writes the wiki LKS or any live
    corpus: ``target`` is a name recorded for the human-merge step, not a path.
    """
    supported = supported_nodes(list(nodes))
    if not supported:
        raise CurateError(
            "propose_crystallize requires at least one supported node (nothing to crystallize)"
        )
    validate_program_id(program_id)

    docs = tuple(_crystallize_doc(n) for n in supported)
    crystallize_id = _content_id(
        "crystallize", program_id, [_node_id(n) for n in supported]
    )
    proposal = CrystallizeProposal(
        program_id=program_id,
        lks_namespace=lks_namespace,
        target=target,
        crystallize_id=crystallize_id,
        docs=docs,
        ts=ts,
    )

    if artifact_dir is not None:
        _write_json_artifact(artifact_dir, proposal.artifact_filename, proposal.to_dict())
        # The doc bundle also lands UNDER artifact_dir (never the live wiki) — proposal artifacts only.
        bundle_dir = pathlib.Path(artifact_dir) / proposal.bundle_dirname
        for doc in docs:
            _write_text_artifact(bundle_dir, f"{doc.slug}.md", doc.body)

    return proposal


# ---------------------------------------------------------------------------
# Orchestrator — one curator pass (sweep, then propose where warranted)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CurateResult:
    """The result of one full curator pass: the sweep + any archive/crystallize proposals."""

    sweep: SweepResult
    archive: Optional[ArchiveProposal]
    crystallize: Optional[CrystallizeProposal]

    def to_dict(self) -> dict:
        return {
            "sweep": self.sweep.to_dict(),
            "archive": self.archive.to_dict() if self.archive else None,
            "crystallize": self.crystallize.to_dict() if self.crystallize else None,
        }


def curate(
    nodes: Sequence[HypothesisNode],
    *,
    program_id: str,
    ts: Any,
    new_evidence: Optional[Mapping[str, int]] = None,
    lks_namespace: str = "research_forest",
    crystallize_target: str = DEFAULT_CRYSTALLIZE_TARGET,
    archive_rationale: Optional[str] = None,
    artifact_dir: Optional[Any] = None,
) -> CurateResult:
    """Run ONE curator pass over a tree — proposal-only (inv. 14), pure but for optional artifacts.

    Steps: (1) dormancy sweep (§7.2, mutates node epoch/status); (2) if the tree is now all
    dormant/resolved, build an archive proposal; (3) if any node is supported, build a crystallize-out
    proposal. With *artifact_dir* set, each proposal writes its artifact(s) there and NOWHERE else.
    Returns a :class:`CurateResult`. Nothing auto-applies; no live corpus is written.
    """
    nodes = list(nodes)
    sweep = sweep_dormancy(nodes, new_evidence)

    archive: Optional[ArchiveProposal] = None
    if is_tree_dormant_or_resolved(nodes):
        archive = propose_archive(
            nodes,
            program_id=program_id,
            ts=ts,
            rationale=archive_rationale,
            artifact_dir=artifact_dir,
        )

    crystallize: Optional[CrystallizeProposal] = None
    if supported_nodes(nodes):
        crystallize = propose_crystallize(
            nodes,
            program_id=program_id,
            ts=ts,
            lks_namespace=lks_namespace,
            target=crystallize_target,
            artifact_dir=artifact_dir,
        )

    return CurateResult(sweep=sweep, archive=archive, crystallize=crystallize)


__all__ = [
    # constants
    "ARCHIVE_APPROVAL_KIND",
    "CRYSTALLIZE_APPROVAL_KIND",
    "DEFAULT_CRYSTALLIZE_TARGET",
    "RESOLVED_STATUSES",
    "INACTIVE_STATUSES",
    # errors
    "CurateError",
    # predicates
    "supported_nodes",
    "is_tree_dormant_or_resolved",
    "ready_to_archive",
    # dormancy sweep
    "DormancyTransition",
    "SweepResult",
    "sweep_dormancy",
    # archive
    "ArchiveProposal",
    "propose_archive",
    # crystallize
    "CrystallizeDoc",
    "CrystallizeProposal",
    "propose_crystallize",
    # orchestrator
    "CurateResult",
    "curate",
]
