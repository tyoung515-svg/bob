"""V1 claim-model v2 — per-type DETERMINISTIC entailment checks (MS#4 · Slice 2, D-2 + F5).

Each check verifies a claim against a SOURCE text with no model. ALL of a claim's types must
pass (F4). Default-FAIL throughout: a missing/failed check is a FAIL, never a silent pass.

CAUSAL is DOWNGRADED per F5: the source must EXPLICITLY contain a causal relation (a closed
lemma) tying the SAME subject and object — no model verdict can override a missing source
relation. The stronger "asserts mechanism, not correlation" judgment is out of the
deterministic slice (it would need a different-family critic, recorded in the ledger). PURE.
"""
from __future__ import annotations

import re

from core.claim.v2.canonical import _get, _norm_text, lemmatize_predicate
from core.claim.v2.taxonomy import CAUSAL_LEMMAS, ClaimType, classify_claim

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z]+")


def _src(source) -> str:
    return _norm_text(source)


def _phrase(v) -> str:
    return _norm_text(v).replace("_", " ")


def _entity_present(entity: str, src: str) -> bool:
    """A normalized entity is present if its phrase is a substring, or all its tokens appear."""
    if not entity:
        return True  # nothing to require
    if entity in src:
        return True
    return all(tok in src for tok in entity.split() if tok)


def check_numeric(claim, source, *, rel_eps: float = 1e-6) -> tuple[bool, str]:
    v = _get(claim, "numeric_value")
    try:
        target = float(v)
    except (TypeError, ValueError):
        return False, "numeric: claim carries no numeric value (Default-FAIL)"
    tol = rel_eps * (1.0 + abs(target))
    for m in _NUM_RE.findall(str(source)):
        try:
            if abs(float(m) - target) <= tol:
                return True, f"numeric: {m} ∈ [{target}±ε]"
        except ValueError:
            continue
    return False, f"numeric: {target} not found in source (Default-FAIL)"


def check_existential(claim, source) -> tuple[bool, str]:
    subj = _phrase(_get(claim, "subject"))
    if _entity_present(subj, _src(source)):
        return True, f"existential: '{subj}' present"
    return False, f"existential: subject '{subj}' absent (Default-FAIL)"


def check_causal(claim, source) -> tuple[bool, str]:
    lemma = lemmatize_predicate(_get(claim, "predicate"))
    if lemma not in CAUSAL_LEMMAS:
        return False, f"causal: '{lemma}' is not a causal lemma (Default-FAIL)"
    src = _src(source)
    subj, obj = _phrase(_get(claim, "subject")), _phrase(_get(claim, "object"))
    causal_present = any(lemmatize_predicate(w) == lemma for w in _WORD_RE.findall(src))
    if causal_present and _entity_present(subj, src) and _entity_present(obj, src):
        return True, f"causal: '{lemma}' ties subject+object in source"
    return False, "causal: no explicit causal relation between the two entities (Default-FAIL)"


def check_definitional(claim, source) -> tuple[bool, str]:
    subj, obj = _phrase(_get(claim, "subject")), _phrase(_get(claim, "object"))
    if not subj or not obj:
        return False, "definitional: needs subject and object (Default-FAIL)"
    pat = rf"{re.escape(subj)}\s+(?:is|are)\s+(?:a\s+|an\s+|the\s+)?{re.escape(obj)}"
    if re.search(pat, _src(source)):
        return True, "definitional: is-a match in source"
    return False, "definitional: no is-a relation in source (Default-FAIL)"


def check_temporal(claim, source) -> tuple[bool, str]:
    lemma = lemmatize_predicate(_get(claim, "predicate"))
    src = _src(source)
    subj, obj = _phrase(_get(claim, "subject")), _phrase(_get(claim, "object"))
    rel_present = any(lemmatize_predicate(w) == lemma for w in _WORD_RE.findall(src))
    if rel_present and _entity_present(subj, src) and _entity_present(obj, src):
        return True, f"temporal: '{lemma}' ordering + entities present"
    return False, "temporal: no explicit ordering in source (Default-FAIL)"


_CHECKS = {
    ClaimType.NUMERIC: check_numeric,
    ClaimType.CAUSAL: check_causal,
    ClaimType.TEMPORAL: check_temporal,
    ClaimType.DEFINITIONAL: check_definitional,
    ClaimType.EXISTENTIAL: check_existential,
}


def check_claim(claim, source, *, types=None) -> tuple[bool, list[str]]:
    """Deterministic per-type entailment: ALL of the claim's types must pass (F4). Default-FAIL:
    an unknown or failing check is a FAIL. Returns (all_passed, [per-type reason])."""
    types = types or classify_claim(claim)
    reasons: list[str] = []
    ok_all = True
    for t in types:
        fn = _CHECKS.get(t)
        if fn is None:
            ok_all = False
            reasons.append(f"{t.value}: no deterministic check (Default-FAIL)")
            continue
        ok, why = fn(claim, source)
        reasons.append(why)
        ok_all = ok_all and ok
    return ok_all, reasons


__all__ = [
    "check_numeric", "check_existential", "check_causal", "check_definitional",
    "check_temporal", "check_claim",
]
