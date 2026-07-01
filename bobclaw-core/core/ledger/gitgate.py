from __future__ import annotations

from core.ledger.erg import on_entailment_failure
from core.ledger.gitdag import merge_synthesis
from core.ledger.mergegate import merge_decision


def run_merge_gate(
    repo: str,
    branch: str,
    verdicts: list[dict],
    *,
    into: str = "main",
    budget_escalated: bool = False,
    erg_entries: dict[str, dict] | None = None,
) -> dict:
    """Execute the §2.6 verification spine as a merge gate.

    Parameters
    ----------
    repo : str
        Path to the git repository.
    branch : str
        Branch being merged.
    verdicts : list[dict]
        Each verdict must have ``verified`` and ``exhausted`` keys.
    into : str, optional
        Target branch (default ``"main"``).
    budget_escalated : bool, optional
        If True the decision is forced to ESCALATE.
    erg_entries : dict[str, dict] | None, optional
        Map from bid_key to prior ERG entry. Defaults to empty dict.

    Returns
    -------
    dict
        Standard merge-gate result with keys:
        * ``decision`` (str): ``"FAST_FORWARD"`` / ``"REVERT"`` / ``"ESCALATE"``.
        * ``action`` (str): one of ``"merged"``, ``"conflict"``, ``"reverted"``, ``"escalate"``.
        * ``merge_result`` (dict, only for FAST_FORWARD): outcome of merge_synthesis.
        * ``erg_directives`` (list[dict], only for REVERT): collected directives.
        * ``reasons`` (list[str]): rationale from merge_decision.
    """
    result = merge_decision(verdicts, budget_escalated)
    decision = result["decision"]
    reasons = result["reasons"]

    if decision == "FAST_FORWARD":
        mr = merge_synthesis(repo, branch, into=into)
        action = "merged" if mr["merged"] else "conflict"
        return {
            "decision": decision,
            "action": action,
            "merge_result": mr,
            "reasons": reasons,
        }

    if decision == "REVERT":
        # REVERT at the merge gate = REFUSE TO MERGE (§2.9 "the merge refusing to fast-forward
        # until criteria are met"), NOT an after-the-fact `git revert`. The branch was never
        # merged, so its history stays intact on the branch and `into` is left untouched; ERG
        # holds the retry state for a decorrelated re-branch. (gitdag.revert_claim is the separate
        # primitive for undoing an ALREADY-committed claim.)
        if erg_entries is None:
            erg_entries = {}
        directives: list[dict] = []
        for v in verdicts:
            satisfied = v.get("verified") is True or v.get("exhausted") is True
            if not satisfied:
                bid_key = v.get("bid_key")
                # erg.on_entailment_failure does NOT mutate its input (Phase-1 contract +
                # regression-tested), so passing the stored entry can't corrupt erg_entries.
                entry = erg_entries.get(
                    bid_key,
                    {
                        "bid_key": bid_key,
                        "retry_count": 0,
                        "tried_sources": [],
                        "status": "PENDING",
                    },
                )
                failure_result = on_entailment_failure(
                    entry, new_source="<merge-gate>"
                )
                # Carry bid_key + the updated entry alongside the directive: an EXHAUSTED_SEARCH
                # directive omits bid_key, so without this the caller can't tell WHICH claim
                # exhausted or persist the new retry_count back into erg_entries.
                directives.append({
                    "bid_key": bid_key,
                    "entry": failure_result["entry"],
                    "directive": failure_result["directive"],
                })
        return {
            "decision": decision,
            "action": "reverted",
            "erg_directives": directives,
            "reasons": reasons,
        }

    # ESCALATE – no git work
    return {
        "decision": decision,
        "action": "escalate",
        "reasons": reasons,
    }
