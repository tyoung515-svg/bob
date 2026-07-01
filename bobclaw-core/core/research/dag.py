"""MS2-R6 — branch-per-run / merge=synthesis / re-branch debate-loop on the §2.9 git-DAG ledger.

Wire the research lane's refute-and-vote rounds (R5 ``core/research/converge.py``) onto the git-DAG: **each
subagent run / round = a branch**, **merge = synthesis**, the **merge gate = the CitationAgent's
``merge_decision``** (Default-FAIL refuses to fast-forward an unverified claim), and **re-branch-from-merge =
the next adversarial round** seeded from ``read_ledger_at(<merge>)``. The **could-not-verify set + per-model
provenance (blame)** stay queryable in history (DESIGN-MS-D2 §2.2).

This module **COMPOSES the landed §2.9 ledger primitives and writes NO new ledger code** — it does not shell
git itself (no ``subprocess`` / ``_git`` here). It CALLS ``gitdag`` (``branch_run`` / ``merge_synthesis`` /
``revert_claim`` / ``head_sha`` / ``current_branch``), ``session``
(``commit_trajectory_with_provenance`` / ``build_provenance_trailers``), ``project`` (``read_ledger_at``),
``mergegate`` (``merge_decision``), and ``gitlog`` (``blame_claim``). The only bytes it writes are DATA (claim
JSON under ``ledger/claims/`` + assertion events in ``ledger/events.jsonl``) — mirroring R3's
``LedgerReportStore``. It operates ONLY on a caller-supplied repo path (a THROWAWAY repo in the E2E), never a
live corpus.

Design (the durable-log / projected-report split that keeps blame queryable across a revert):
  * The **event log** (``events.jsonl``) is the immutable assertion ledger — one ``claim_assertion`` event per
    claim asserted in a round (surviving ``verified=True`` + contested ``verified=False``), each carrying
    ``round`` / ``asserter`` / ``targets:[{claim}]``. It is written in the round's ONE ``ARTIFACT_COMPLETE``
    trajectory commit and is NEVER reverted → ``blame_claim`` / ``read_ledger_at`` find every claim (surviving
    OR reverted). This is where "which model first asserted X" + the could-not-verify set live.
  * The **projected report** (``claims/*.json``) is the ACCEPTED (synthesized, surviving) claims. Surviving
    claim files ride the artifact commit; a CONTESTED claim file is committed then ``revert_claim``-ed (the
    Default-FAIL revert-with-reason) so it is absent from the report tree but its revert pair stays in history.

v1 scope (§5 non-goal): commits research artifacts at boundaries; the projection-only write path is v2.
Import-light: no network/Qdrant at import; no concrete model name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from core.ledger.gitdag import (
    branch_run,
    merge_synthesis,
    revert_claim,
    head_sha,
    current_branch,
    GitError,
)
from core.ledger.session import (
    commit_trajectory_with_provenance,
    build_provenance_trailers,
)
from core.ledger.project import read_ledger_at
from core.ledger.mergegate import merge_decision
from core.ledger.gitlog import blame_claim
from core.ledger.types import BoundaryKind, EXHAUSTED_TAG

logger = logging.getLogger("bobclaw.research.dag")


class ResearchDagError(RuntimeError):
    """Construction/config misuse: empty repo/date/slug, a negative round index."""
    pass


# ── A) PURE verdict adapter + round inputs (NO git) ────────────────────────────

def _get(obj: Any, attr: str, default: Any = None) -> Any:
    """Duck-typed getter — works on a dict (by key) or an object (by attribute)."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _bid(claim: Any) -> str:
    """The bid_key from a Claim-like (``.bid_key``) or a dict (``["bid_key"]``); else ``subject|predicate|numeric_value``."""
    if not isinstance(claim, dict) and hasattr(claim, "bid_key"):
        return str(claim.bid_key)
    if isinstance(claim, dict):
        bk = claim.get("bid_key")
        if bk:
            return str(bk)
    return f"{_get(claim, 'subject', '')}|{_get(claim, 'predicate', '')}|{_get(claim, 'numeric_value', '')}"


def _ascii_safe(text: str, *, limit: int = 160) -> str:
    """Fold to ASCII (NFKD, mirroring ``gitdag.normalize_slug``), collapse whitespace/newlines, and truncate.

    A git ``-m`` commit message is passed through ``subprocess`` argv; free-text ``reason`` (LLM-generated) with
    an em-dash / newline / oversized body has hit a real Windows argv hazard in this repo (CLAUDE.md gotcha).
    """
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded[:limit]


def _is_exhausted(entry: Mapping) -> bool:
    """True iff the entry's ``kind`` (uppercased) contains 'EXHAUST' or any reason names EXHAUSTED_SEARCH."""
    if "EXHAUST" in str(entry.get("kind", "")).upper():
        return True
    for r in entry.get("reasons", []) or []:
        s = str(r)
        if EXHAUSTED_TAG in s or "EXHAUSTED_SEARCH" in s:
            return True
    return False


def verdicts_from_round(
    surviving_keys: Sequence[str],
    could_not_verify: Sequence[Mapping],
) -> list[dict]:
    """Build the mergegate feed: each surviving key -> ``{verified:True,exhausted:False}``; each could-not-verify
    key -> ``{verified:False,exhausted:<EXHAUSTED_SEARCH?>}``. De-dup by bid_key (SURVIVING wins). PURE."""
    out: list[dict] = []
    seen: set[str] = set()
    for key in surviving_keys:
        if key and key not in seen:
            seen.add(key)
            out.append({"bid_key": key, "verified": True, "exhausted": False})
    for entry in could_not_verify:
        key = entry.get("bid_key")
        if key and key not in seen:
            seen.add(key)
            out.append({"bid_key": key, "verified": False, "exhausted": _is_exhausted(entry)})
    return out


def _norm_claim(claim: Any) -> dict:
    """A dict copy with ``subject/predicate/numeric_value/cited_source_id/text`` + a filled ``bid_key``. PURE."""
    return {
        "subject": _get(claim, "subject", ""),
        "predicate": _get(claim, "predicate", ""),
        "numeric_value": _get(claim, "numeric_value", None),
        "cited_source_id": _get(claim, "cited_source_id", None),
        "text": _get(claim, "text", "") or "",
        "bid_key": _bid(claim),
    }


@dataclass(frozen=True)
class RoundInputs:
    """One round's DAG inputs: all candidate claims + the R5 surviving/could-not-verify split + asserter/budget."""
    claims: tuple = field(default_factory=tuple)              # tuple[dict] (subject/predicate/numeric_value/…/bid_key)
    surviving_keys: tuple = field(default_factory=tuple)      # tuple[str]
    could_not_verify: tuple = field(default_factory=tuple)    # tuple[dict] {bid_key,kind,reasons,round}
    asserter_backend: str = ""
    budget_escalated: bool = False


def round_inputs_from_converge(converge_result: Any, claims: Sequence[Any]) -> RoundInputs:
    """Duck-typed PURE adapter from an R5 ConvergeResult (+ the round's candidate claims) into ``RoundInputs``.

    Reads ``surviving_keys`` / ``could_not_verify`` / ``asserter_backend`` / ``budget_bound`` off the result;
    normalizes each claim (filling ``bid_key``). Does NOT import ``converge``.
    """
    return RoundInputs(
        claims=tuple(_norm_claim(c) for c in claims),
        surviving_keys=tuple(getattr(converge_result, "surviving_keys", ()) or ()),
        could_not_verify=tuple(getattr(converge_result, "could_not_verify", ()) or ()),
        asserter_backend=str(getattr(converge_result, "asserter_backend", "") or ""),
        budget_escalated=bool(getattr(converge_result, "budget_bound", False)),
    )


# ── B) Result carriers ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RoundCommit:
    """One round onto the DAG: the synthesis trajectory commit + the Default-FAIL revert trail + the merge."""
    round_idx: int
    branch: str
    seed_ref: str
    artifact_sha: Optional[str]     # the ONE ARTIFACT_COMPLETE trajectory commit (assertion log + surviving report)
    decision: dict                  # mergegate.merge_decision(...) -> {"decision","reasons"}
    surviving_keys: tuple           # bid_keys synthesized into the report this round
    contested: tuple                # [{"bid_key","kind","reasons","assert_sha","revert_sha"}]
    merge_sha: Optional[str]        # the synthesis merge commit (the next round's re-branch seed)
    merged: bool
    conflicts: tuple
    escalated: bool                 # decision == ESCALATE (budget-contested-by-cost, §2.7)

    def as_dict(self) -> dict:
        return {
            "round_idx": self.round_idx,
            "branch": self.branch,
            "seed_ref": self.seed_ref,
            "artifact_sha": self.artifact_sha,
            "decision": self.decision,
            "surviving_keys": list(self.surviving_keys),
            "contested": [dict(c) for c in self.contested],
            "merge_sha": self.merge_sha,
            "merged": self.merged,
            "conflicts": list(self.conflicts),
            "escalated": self.escalated,
        }


@dataclass(frozen=True)
class DagResult:
    """The multi-round run: the per-round commits + the final report ref + the cumulative surviving/could-not-verify."""
    rounds: tuple                   # tuple[RoundCommit]
    base_branch: str
    final_ref: str                  # the report HEAD (the last merge sha, or the base HEAD if nothing merged)
    surviving_keys: tuple           # cumulative bid_keys present in read_ledger_at(final_ref).claims (first-seen order)
    could_not_verify: tuple         # cumulative contested {bid_key,kind,reasons,round}
    escalated: bool = False         # any round was budget-ESCALATE (contested-by-cost, §2.7 — surfaced to the caller)

    def as_dict(self) -> dict:
        return {
            "rounds": [rc.as_dict() for rc in self.rounds],
            "base_branch": self.base_branch,
            "final_ref": self.final_ref,
            "surviving_keys": list(self.surviving_keys),
            "could_not_verify": [dict(e) for e in self.could_not_verify],
            "escalated": self.escalated,
        }


# ── C) The DAG (composes the landed primitives; the only bytes it writes are DATA) ──

class ResearchDag:
    """Wire the research rounds onto the §2.9 git-DAG by composing the landed ledger primitives."""

    def __init__(
        self,
        repo: Any,
        *,
        date: str,
        slug: str,
        base_branch: Optional[str] = None,
        ledger_dir: str = "ledger",
    ) -> None:
        """Bind the THROWAWAY repo, the date + slug (branch namespacing), the base/report branch, the ledger sub-dir."""
        repo_str = str(repo)
        date_str = str(date)
        slug_str = str(slug)
        if not repo_str or not date_str or not slug_str:
            raise ResearchDagError("repo, date and slug must all be non-empty")
        self._repo = Path(repo_str)
        self._date = date_str
        self._slug = slug_str
        self._ledger_dir = ledger_dir
        self._base_branch = base_branch or current_branch(self._repo)

    # -- data-only helpers (write claim JSON + append events; NO git) --

    def _claims_dir(self) -> Path:
        return self._repo / self._ledger_dir / "claims"

    def _events_file(self) -> Path:
        return self._repo / self._ledger_dir / "events.jsonl"

    def _stem(self, bid_key: str) -> str:
        """A filesystem-safe, stable stem for a claim file (bid_key may carry '|' / ':' / spaces)."""
        return hashlib.sha256(bid_key.encode("utf-8")).hexdigest()[:12]

    def _write_claim_file(self, claim: dict) -> None:
        """Write ``claims/<stem>.json`` = the claim dict stamped with ``"id"=bid_key`` (so read_ledger_at keys by bid_key)."""
        bid_key = claim["bid_key"]
        record = dict(claim)
        record["id"] = bid_key
        path = self._claims_dir() / f"{self._stem(bid_key)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def _append_event(self, claim: dict, *, round_idx: int, asserter: str, verified: bool) -> None:
        """Append ONE ``claim_assertion`` event to ``events.jsonl`` (the durable, never-reverted assertion log)."""
        bid_key = claim["bid_key"]
        statement = claim.get("text") or f"{claim.get('subject','')} {claim.get('predicate','')} {claim.get('numeric_value','')}"
        event = {
            "id": f"assert-{round_idx}-{self._stem(bid_key)}",
            "kind": "claim_assertion",
            "round": round_idx,
            "asserter": asserter,
            "verified": verified,
            "targets": [{"claim": bid_key}],
            "statement": statement,
        }
        events_file = self._events_file()
        events_file.parent.mkdir(parents=True, exist_ok=True)
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    # -- the primitives, composed --

    def seed(self, ref: str) -> dict:
        """The evolving report (truth-at-a-commit) via ``project.read_ledger_at`` — the re-branch-from-merge seed."""
        return read_ledger_at(self._repo, ref, ledger_dir=self._ledger_dir)

    def commit_round(self, round_idx: int, inputs: RoundInputs, *, seed_ref: str) -> RoundCommit:
        """ONE round onto the DAG: branch off ``seed_ref`` -> record the assertion log + synthesize survivors in ONE
        ARTIFACT_COMPLETE commit -> gate (``merge_decision``) -> on Default-FAIL REVERT, git-revert each contested
        claim WITH its reason -> ``merge_synthesis`` into the base branch. Leaves HEAD on the base branch."""
        if round_idx < 0:
            raise ResearchDagError("round_idx must be >= 0")

        branch = branch_run(self._repo, f"{self._slug}-r{round_idx}", date=self._date, base=seed_ref)

        claims = [_norm_claim(c) for c in inputs.claims]
        surviving_set = set(inputs.surviving_keys)
        contested_set = {e.get("bid_key") for e in inputs.could_not_verify}
        surviving = [c for c in claims if c["bid_key"] in surviving_set]
        contested_claims = [c for c in claims if c["bid_key"] in contested_set and c["bid_key"] not in surviving_set]

        # FAIL CLOSED: a surviving_key with no matching claim would be marked verified by the merge gate yet have
        # NO file in the report tree — a silently-dropped survivor. Refuse (the integrity invariant: every bid_key
        # the gate accepts as verified MUST have a claims/*.json file in the merge tree).
        missing = surviving_set - {c["bid_key"] for c in surviving}
        if missing:
            raise ResearchDagError(
                f"round {round_idx}: {len(missing)} surviving_keys have no matching claim {sorted(missing)} — "
                f"refusing (a verified survivor must have a claim file in the report tree, never a silent drop)"
            )

        # 1) the ONE trajectory commit: the immutable assertion log (each ASSERTED claim's event, never reverted) +
        #    the synthesized surviving-claim FILES (the projected report). One squashed commit per trajectory.
        wrote_any_event = False
        for c in claims:
            # only a claim R5 actually classified (surviving OR could-not-verify) gets a verdict-bearing event;
            # a neutral/untracked claim is NOT durably mislabeled as unverified.
            if c["bid_key"] in surviving_set or c["bid_key"] in contested_set:
                self._append_event(c, round_idx=round_idx, asserter=inputs.asserter_backend,
                                   verified=(c["bid_key"] in surviving_set))
                wrote_any_event = True
        for c in surviving:
            self._write_claim_file(c)

        artifact_sha = None
        if wrote_any_event:
            trailers = build_provenance_trailers(git_branch=branch)
            trailers += [f"Research-Round: {round_idx}", f"Research-Asserter: {inputs.asserter_backend}"]
            artifact_sha = commit_trajectory_with_provenance(
                self._repo,
                f"research: round {round_idx} synthesis ({len(surviving)} verified, {len(contested_claims)} contested)",
                trailers=trailers,
                paths=[self._ledger_dir],
                boundary_kind=BoundaryKind.ARTIFACT_COMPLETE.value,
            )

        # 2) the merge gate (Default-FAIL): the CitationAgent verdicts -> mergegate.merge_decision.
        verdicts = verdicts_from_round(inputs.surviving_keys, inputs.could_not_verify)
        decision = merge_decision(verdicts, budget_escalated=inputs.budget_escalated)
        escalated = decision["decision"] == "ESCALATE"

        # 3) Default-FAIL REVERT: a contested claim must NOT fast-forward into the report.
        #    * a claim NEW to this branch -> assert (add the file) then git-revert it (a `Revert "..."` in history);
        #    * a claim that SURVIVED a prior round (its file rode `seed_ref`) and is NOW contested -> DELETE the file
        #      with the reason. (Re-writing identical deterministic content would be a NO-DIFF no-op and leave the
        #      prior survivor in the report — the debate-loop breach: `run()` re-branches each round from the prior
        #      merge, so a later adversarial round routinely re-contests an already-merged survivor.)
        #    Either way the verified=False assertion EVENT already rode the artifact commit (never reverted) -> the
        #    contested claim stays blame-traceable.
        contested: list[dict] = []
        if decision["decision"] == "REVERT":
            for c in contested_claims:
                entry = next((e for e in inputs.could_not_verify if e.get("bid_key") == c["bid_key"]), None)
                kind = (entry.get("kind") if entry else None) or "unverified"
                reasons = list((entry.get("reasons") if entry else None) or [kind])
                reason = reasons[0] if reasons else kind
                claim_path = self._claims_dir() / f"{self._stem(c['bid_key'])}.json"
                pre_existing = claim_path.exists()   # a prior-round survivor carried in via seed_ref
                msg = _ascii_safe(f"research: contested claim {c['bid_key']} REVERTED: {reason}", limit=200)
                assert_sha = revert_sha = None
                if pre_existing:
                    claim_path.unlink()              # a real diff (deletion) -> removes the now-contested survivor
                    revert_sha = commit_trajectory_with_provenance(
                        self._repo, msg, paths=[self._ledger_dir], boundary_kind=BoundaryKind.CORRECTION.value)
                else:
                    self._write_claim_file(c)
                    assert_sha = commit_trajectory_with_provenance(
                        self._repo, msg, paths=[self._ledger_dir], boundary_kind=BoundaryKind.CORRECTION.value)
                    if assert_sha:
                        try:
                            revert_sha = revert_claim(self._repo, assert_sha)
                        except GitError as exc:  # noqa: BLE001 — a failed revert is a HARD stop below, never silent
                            logger.warning("revert_claim failed for %s (%s): %s", c["bid_key"], assert_sha, exc)
                contested.append({"bid_key": c["bid_key"], "kind": kind, "reasons": reasons, "pre_existing": pre_existing,
                                  "assert_sha": assert_sha, "revert_sha": revert_sha})

            # FAIL CLOSED (direct invariant check on the COMMITTED tree that merge_synthesis will fold — not the
            # working tree): if ANY contested claim is still present at the branch HEAD, the merge would carry it
            # into the report (Default-FAIL breach) -> ABORT the round. Catches a failed git-revert, an uncommitted
            # deletion, AND the no-diff no-op case, independent of commit-sha bookkeeping.
            committed = read_ledger_at(self._repo, "HEAD", ledger_dir=self._ledger_dir).get("claims", {})
            still_present = [c["bid_key"] for c in contested_claims if c["bid_key"] in committed]
            if still_present:
                raise ResearchDagError(
                    f"round {round_idx}: contested claim(s) still present in the report tree {still_present} — "
                    f"refusing to merge (Default-FAIL: a contested claim must never fast-forward into the report)"
                )

        # 4) merge = synthesis: fold the round branch into the report base (the merge tree carries the survivors).
        merge_sha = None
        merged = False
        conflicts: tuple = ()
        if artifact_sha is not None or contested:
            result = merge_synthesis(self._repo, branch, into=self._base_branch)
            merge_sha = result.get("commit")
            merged = bool(result.get("merged"))
            conflicts = tuple(result.get("conflicts") or [])

        return RoundCommit(
            round_idx=round_idx,
            branch=branch,
            seed_ref=seed_ref,
            artifact_sha=artifact_sha,
            decision=decision,
            surviving_keys=tuple(c["bid_key"] for c in surviving),
            contested=tuple(contested),
            merge_sha=merge_sha,
            merged=merged,
            conflicts=conflicts,
            escalated=escalated,
        )

    def run(self, rounds: Sequence[RoundInputs]) -> DagResult:
        """Drive N rounds. Round 0 seeds off the base HEAD; round k>=1 RE-BRANCHES FROM the prior round's merge
        commit (``seed_ref = rounds[k-1].merge_sha``, seeded via ``read_ledger_at(that merge)``)."""
        results: list[RoundCommit] = []
        seed_ref = head_sha(self._repo)
        cnv_accum: list[dict] = []
        for i, inp in enumerate(rounds):
            rc = self.commit_round(i, inp, seed_ref=seed_ref)
            results.append(rc)
            if rc.merge_sha:                       # RE-BRANCH-FROM-MERGE: the next round branches off this merge
                seed_ref = rc.merge_sha
            for e in inp.could_not_verify:
                cnv_accum.append({**dict(e), "round": i})

        final_ref = seed_ref if results else head_sha(self._repo)
        final_claims = read_ledger_at(self._repo, final_ref, ledger_dir=self._ledger_dir).get("claims", {})
        seen: set[str] = set()
        cumulative: list[str] = []
        for rc in results:
            for bk in rc.surviving_keys:
                if bk in final_claims and bk not in seen:
                    seen.add(bk)
                    cumulative.append(bk)

        return DagResult(
            rounds=tuple(results),
            base_branch=self._base_branch,
            final_ref=final_ref,
            surviving_keys=tuple(cumulative),
            could_not_verify=tuple(cnv_accum),
            escalated=any(rc.escalated for rc in results),
        )

    def blame(self, claim_id: str) -> list[dict]:
        """"Which model first asserted X" — compose ``gitlog.blame_claim`` (commit/date) + ``read_ledger_at``
        events (round/asserter). Ordered by round then commit; the FIRST entry is the first asserter. [] if unknown."""
        try:
            blame_entries = blame_claim(self._repo, claim_id, events_path=f"{self._ledger_dir}/events.jsonl")
        except GitError:
            blame_entries = []
        try:
            events = read_ledger_at(self._repo, "HEAD", ledger_dir=self._ledger_dir).get("events", [])
        except GitError:
            events = []

        by_event_id: dict[str, dict] = {}
        for ev in events:
            if any(isinstance(t, dict) and t.get("claim") == claim_id for t in ev.get("targets", [])):
                eid = ev.get("id")
                if eid:
                    by_event_id[eid] = ev

        out: list[dict] = []
        for entry in blame_entries:
            ev = by_event_id.get(entry.get("event_id"))
            out.append({
                "claim": claim_id,
                "round": ev.get("round") if ev else None,
                "asserter": ev.get("asserter") if ev else None,
                "commit": entry.get("commit"),
                "date": entry.get("date"),
                "statement": entry.get("statement", ""),
                "verified": ev.get("verified") if ev else None,
                "event_id": entry.get("event_id"),
            })
        if not out:  # fall back to the event log directly (e.g. a missing/short blame)
            for ev in events:
                if any(isinstance(t, dict) and t.get("claim") == claim_id for t in ev.get("targets", [])):
                    out.append({
                        "claim": claim_id, "round": ev.get("round"), "asserter": ev.get("asserter"),
                        "commit": None, "date": None, "statement": ev.get("statement", ""),
                        "verified": ev.get("verified"), "event_id": ev.get("id"),
                    })

        out.sort(key=lambda x: (x["round"] if x.get("round") is not None else 10 ** 9, x.get("commit") or ""))
        return out


__all__ = [
    "ResearchDagError",
    "RoundInputs",
    "RoundCommit",
    "DagResult",
    "ResearchDag",
    "verdicts_from_round",
    "round_inputs_from_converge",
]
