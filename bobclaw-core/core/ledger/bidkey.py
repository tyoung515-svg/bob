from __future__ import annotations

"""
Canonical claim‑identity key for the BoBClaw LKS git-DAG ledger (§2.6 F1).

Pure, deterministic, low‑entropy normalisation: normalise subject, lemmatise
predicate via a fixed synonym map and heuristic suffix stripping, round numeric
values to significant figures, and hash the resulting triple.
"""

import hashlib
import json
import re
import string
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from core.ledger.types import BID_NUMERIC_NDIGITS

# -- Module‑level constants ---------------------------------------------------

SYNONYM_MAP: dict[str, str] = {
    "scores": "score",
    "scored": "score",
    "scoring": "score",
    "achieves": "achieve",
    "achieved": "achieve",
    "achieving": "achieve",
    "contains": "contain",
    "contained": "contain",
    "containing": "contain",
    "produces": "produce",
    "produced": "produce",
    "producing": "produce",
    "requires": "require",
    "required": "require",
    "requiring": "require",
    "evaluates": "evaluate",
    "evaluated": "evaluate",
    "evaluating": "evaluate",
    "generates": "generate",
    "generated": "generate",
    "generating": "generate",
    "matches": "match",
    "matched": "match",
    "matching": "match",
    "works": "work",
    "worked": "work",
    "working": "work",
    "calls": "call",
    "called": "call",
    "calling": "call",
    "uses": "use",
    "used": "use",
    "using": "use",
    "executes": "execute",
    "executed": "execute",
    "executing": "execute",
    "builds": "build",
    "built": "build",
    "building": "build",
}

_LEADING_ARTICLE_PATTERN = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_PUNCTUATION_STRIP = string.punctuation
_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_INFLECTION_SUFFIXES = ["ing", "ed", "es", "s"]
# A lone trailing 's' here is NOT a plural/3rd-person inflection — stripping it would
# manufacture a non-word stem (process->proces, address->addres, status->statu) and break
# consistency with the -ed/-ing form (process vs processed). Skip the bare-'s' strip for these.
_NON_PLURAL_S_ENDINGS = ("ss", "us", "is", "os", "as")


# -- Helper: round to significant figures via Decimal -------------------------

def _round_to_sigfigs(value_str: str, sig: int) -> str:
    """Round a numeric string to *sig* significant figures (ROUND_HALF_UP), returning a
    minimal plain-decimal string (never scientific notation)."""
    try:
        d = Decimal(value_str)
    except Exception:
        return value_str  # fallback, should not happen
    if d.is_zero():
        return "0"
    # Keep `sig` significant digits: quantize at exponent (msd_exponent - sig + 1). Build the
    # quantizer with the correct EXPONENT via scaleb — Decimal(10)**n has exponent 0 and would
    # quantize integers to the units place, so large integers would never sig-fig round.
    quant_exp = d.adjusted() - sig + 1
    quantizer = Decimal(1).scaleb(quant_exp)
    rounded = d.quantize(quantizer, rounding=ROUND_HALF_UP)
    # Plain-decimal, minimal: force fixed-point (no exponent), then strip trailing zeros / point.
    s = format(rounded, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


# -- Public functions ---------------------------------------------------------

def normalize_subject(subject: str) -> str:
    """
    Canonical form for a claim subject.
    
    Steps:
    1. Unicode NFKC normalisation.
    2. Case folding.
    3. Strip leading/trailing whitespace.
    4. Collapse all internal whitespace to a single space.
    5. Drop a leading article (the / a / an + space).
    6. Strip surrounding punctuation (str.strip with string.punctuation).
    7. Final strip of whitespace.
    """
    s = unicodedata.normalize("NFKC", subject)
    s = s.casefold()
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    s = _LEADING_ARTICLE_PATTERN.sub("", s)
    s = s.strip(_PUNCTUATION_STRIP)
    s = s.strip()
    return s


def predicate_lemma(predicate: str) -> str:
    """
    Lemmatised canonical form for a claim predicate.
    
    1. Normalise (casefold, strip, collapse whitespace).
    2. Apply fixed synonym map (module‑level SYNONYM_MAP).
    3. If not mapped, strip a trailing inflection suffix (ing / ed / es / s)
       only if the remaining stem length >= 3.
    """
    p = predicate.casefold().strip()
    p = re.sub(r"\s+", " ", p)
    # Synonym map lookup
    lemma = SYNONYM_MAP.get(p)
    if lemma is not None:
        return lemma
    # Heuristic suffix stripping (longest first)
    for suffix in _INFLECTION_SUFFIXES:
        if p.endswith(suffix):
            if suffix == "s" and p.endswith(_NON_PLURAL_S_ENDINGS):
                continue  # not an inflection (process/status/analysis/address)
            candidate = p[: -len(suffix)]
            if len(candidate) >= 3:
                return candidate
    # No change
    return p


def round_numeric(
    value: object, ndigits: int = BID_NUMERIC_NDIGITS
) -> str | None:
    """
    Extract the first parseable number from *value* and round to *ndigits* significant figures.
    
    Accepts int, float, or str (e.g. "80.40", "80.4%", "  77.8 ").
    Returns a canonical minimal string, or ``None`` if no number can be extracted.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        num_str = str(value)
    elif isinstance(value, str):
        match = _NUMBER_PATTERN.search(value.strip())
        if not match:
            return None
        num_str = match.group()
    else:
        return None
    # Handle special case of empty / non‑numeric string
    if not num_str:
        return None
    return _round_to_sigfigs(num_str, ndigits)


def bid_key(subject: str, predicate: str, numeric_value: object = None) -> str:
    """
    Deterministic SHA‑256 hex digest for a claim triple.
    
    Concatenation: ``normalize_subject(subject) + "|" + predicate_lemma(predicate) + "|" + (round_numeric(numeric_value) or "")``
    """
    # Serialize the triple with json (not a "|".join): a literal "|" inside a normalized
    # subject/predicate can't forge the field boundary — json quotes + escapes each component,
    # so ("a|b","c") and ("a","b|c") hash distinctly. Deterministic (fixed separators).
    triple = [
        normalize_subject(subject),
        predicate_lemma(predicate),
        round_numeric(numeric_value) or "",
    ]
    raw = json.dumps(triple, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
