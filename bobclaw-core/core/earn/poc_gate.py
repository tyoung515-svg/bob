"""E1 — PoC-entailment COMPOUND gate (MS#4 · Slice 1, FM-1). Sets ``poc_reproduced``.

The red-team's load-bearing catch: I5 "reuse the entailment engine" is INSUFFICIENT.
``entailment.py`` checks that a citation textually ENTAILS the narrative; a PoC that entails the
story but never actually triggers the vuln is the false positive I5 exists to kill. So the PoC
gate is a COMPOUND: (a) narrative entailment (unchanged) AND (b) a DAG-anchored reproduction log
whose ``observed_severity == claimed_severity`` (an execution artifact, not a parallel verifier).
Default-FAIL: any part missing/mismatched ⇒ ``poc_reproduced = False`` ⇒ the bounty can never
fast-approve (D-2). PURE.
"""
from __future__ import annotations

from typing import Optional

from core.verify.entailment import EntailmentVerdict


def _is_entailed(verdict) -> bool:
    if isinstance(verdict, EntailmentVerdict):
        return verdict is EntailmentVerdict.ENTAILED
    return str(verdict).strip().lower() == EntailmentVerdict.ENTAILED.value


def evaluate_poc(
    narrative_verdict,
    observed_severity: Optional[str],
    claimed_severity: Optional[str],
) -> bool:
    """Compound FM-1 gate → the value for ``poc_reproduced``.

    True IFF (a) the narrative is ENTAILED AND (b) the reproduction log's ``observed_severity``
    equals the ``claimed_severity`` (both present). Default-FAIL on anything missing/mismatched —
    never a pass on a partial signal.
    """
    if not _is_entailed(narrative_verdict):
        return False
    if not observed_severity or not claimed_severity:
        return False
    return str(observed_severity).strip().lower() == str(claimed_severity).strip().lower()


__all__ = ["evaluate_poc"]
