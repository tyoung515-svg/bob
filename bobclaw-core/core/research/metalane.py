"""RS2 meta-lane — deterministic claim schema + JSONL ledger + Step-0 measures (MS#4 · Step 0).

The recursive compounding loop escapes the verification-gap ceiling by making claim identity
DETERMINISTIC: a CLOSED predicate set + a CORPUS-ANCHORED object (``file:line-range`` or a
``claim:<id>`` reference or a fixed-vocab value) — NEVER free text (SPEC §0). The ONLY new
executable primitive is the JSONL ledger (I-LEDGER); everything else is wiring over
``core/council`` + Send + ``core/verify``. Step 0 measures schema RECALL (fail-fast < 0.70) and
the corpus-anchorable fraction (the < 0.40 go/no-go). Shares the V1 claim-identity discipline.

BLOCKED (needs Travis, §6): adopt the corpus-anchor constraint; ratify the closed predicate set;
the pilot task; the per-generation budget cap; the termination path. The mechanism is built on the
PROPOSED predicate set; the pilot RUN + the go/no-go verdict await those ratifications.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# PROPOSED closed predicate set (SPEC §2 candidate — ratification is a Travis decision, §6).
CLOSED_PREDICATES = frozenset({
    "implements", "depends_on", "violates", "entails", "refutes", "subsumes",
    "conflicts_with", "satisfies",
})
# Fixed object vocabulary allowed alongside corpus anchors (deterministic, closed).
FIXED_VOCAB = frozenset({"null", "empty", "bounded"})
_ANCHOR_RE = re.compile(r"^[^:\s]+:\d+(?:-\d+)?$")   # file:line or file:line-range


def is_corpus_anchor(obj: str) -> bool:
    """True iff ``obj`` is a deterministic corpus anchor: ``file:line[-range]``, a ``claim:<id>``
    reference, or a fixed-vocab value. Free text is NOT anchorable (§0)."""
    if not isinstance(obj, str) or not obj:
        return False
    return bool(_ANCHOR_RE.match(obj)) or obj.startswith("claim:") or obj in FIXED_VOCAB


@dataclass(frozen=True)
class MetaClaim:
    subject: str
    predicate: str
    object: str
    scope: str
    evidence_anchor: str = ""

    @property
    def claim_id(self) -> str:
        """Deterministic identity = hash(subject, predicate, object, scope) — no model canonicalization."""
        blob = json.dumps([self.subject, self.predicate, self.object, self.scope],
                          separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def schema_valid(self) -> bool:
        """A well-formed meta-claim: closed predicate + corpus-anchored object."""
        return self.predicate in CLOSED_PREDICATES and is_corpus_anchor(self.object)


def schema_recall(gold: Iterable["MetaClaim"], extracted: Iterable["MetaClaim"]) -> float:
    """Fraction of gold claims recovered by identity among the SCHEMA-VALID extracted claims
    (a malformed extracted claim doesn't count). 0.0 for an empty gold set."""
    gold_ids = {c.claim_id for c in gold}
    if not gold_ids:
        return 0.0
    got = {c.claim_id for c in extracted if c.schema_valid()}
    return len(gold_ids & got) / len(gold_ids)


def corpus_anchorable_fraction(claims: Iterable["MetaClaim"]) -> float:
    """Fraction of claims whose object is a corpus anchor — the §0 go/no-go input (< 0.40 ⇒ STOP)."""
    claims = list(claims)
    if not claims:
        return 0.0
    return sum(1 for c in claims if is_corpus_anchor(c.object)) / len(claims)


class MetaLedger:
    """The one new primitive: a git-tracked JSONL ledger of verified claims per generation."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    def append(self, generation: int, claim: "MetaClaim", *, verified: bool) -> None:
        rec = {"generation": generation, "claim_id": claim.claim_id, "subject": claim.subject,
               "predicate": claim.predicate, "object": claim.object, "scope": claim.scope,
               "verified": bool(verified)}
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def records(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [json.loads(ln) for ln in self._path.read_text(encoding="utf-8").splitlines() if ln]

    def verified_claims(self, generation: int) -> list[dict]:
        return [r for r in self.records() if r["generation"] == generation and r["verified"]]

    def should_halt(self, generation: int) -> bool:
        """Hard kill (ARCH-05): halt if a generation yielded ≤1 verified claim OR ≤1 distinct anchor
        (no compounding surface). Deterministic on ledger data."""
        vc = self.verified_claims(generation)
        anchors = {r["object"] for r in vc}
        return len(vc) <= 1 or len(anchors) <= 1


__all__ = [
    "CLOSED_PREDICATES", "FIXED_VOCAB", "is_corpus_anchor", "MetaClaim",
    "schema_recall", "corpus_anchorable_fraction", "MetaLedger",
]
