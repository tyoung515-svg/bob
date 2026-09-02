"""PURE hypothesis module for the BoBClaw Research Forest (MS9-F2).

Defines the hypothesis-node schema (SPEC §1), a Beta(alpha, beta) confidence update, falsifier
predicates, and the status machine (SPEC §7.2). This module is PURE: no I/O (no file, network,
database, clock, or random). Callers supply all time/epoch counters — epoch advancement and the
dormancy counter are driven from outside via ``advance_epoch``.

Status machine (SPEC §7.2, ratified constants below), in precedence order:
  * refuted  ⇐ a falsifier fired  OR  posterior mean ≤ ``REFUTED_POSTERIOR_MAX``   (overrides Beta)
  * supported ⇐ posterior mean ≥ ``SUPPORTED_POSTERIOR_MIN`` AND ≥ ``SUPPORTED_MIN_OBSERVATIONS`` obs
  * dormant  ⇐ ``DORMANT_EPOCHS`` consecutive epochs with zero new evidence for the node
  * open     ⇐ otherwise
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.forest.events import ForestError

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------
class HypothesisError(ForestError):
    """Raised on invalid construction of hypothesis nodes."""
    pass

# ---------------------------------------------------------------------------
# Ratified named constants (SPEC §7.2 defaults)
# ---------------------------------------------------------------------------
SUPPORTED_POSTERIOR_MIN = 0.9
SUPPORTED_MIN_OBSERVATIONS = 5
REFUTED_POSTERIOR_MAX = 0.1
DORMANT_EPOCHS = 3
DEFAULT_WEIGHT = 1

# ---------------------------------------------------------------------------
# Claim kinds (SPEC §7.1)
# ---------------------------------------------------------------------------
class ClaimKind(str, Enum):
    LEVEL = "level"
    TREND = "trend"
    COMPARISON = "comparison"
    CAUSAL_CANDIDATE = "causal-candidate"

CLAIM_KINDS = frozenset(k.value for k in ClaimKind)

# ---------------------------------------------------------------------------
# Node status
# ---------------------------------------------------------------------------
class NodeStatus(str, Enum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    DORMANT = "dormant"

NODE_STATUSES = frozenset(s.value for s in NodeStatus)

STATUS_OPEN = "open"
STATUS_SUPPORTED = "supported"
STATUS_REFUTED = "refuted"
STATUS_DORMANT = "dormant"

# ---------------------------------------------------------------------------
# Verification tags (default-FAIL meaning default "U")
# ---------------------------------------------------------------------------
TAGS = frozenset({"PV", "VS", "U"})

# ---------------------------------------------------------------------------
# Beta distribution dataclass
# ---------------------------------------------------------------------------
@dataclass
class Beta:
    alpha: float = 1.0
    beta: float = 1.0

    def mean(self) -> float:
        total = self.alpha + self.beta
        return 0.0 if total <= 0 else self.alpha / total

# ---------------------------------------------------------------------------
# HypothesisNode dataclass (SPEC §1 schema + internal counters)
# ---------------------------------------------------------------------------
@dataclass
class HypothesisNode:
    id: str
    question: str = ""
    hypothesis: str = ""
    claim_kind: str = ClaimKind.LEVEL.value
    falsifiers: list = field(default_factory=list)
    measurement_source: str | None = None
    decision_rule: str | None = None
    evidence_refs: list = field(default_factory=list)
    tag: str = "U"
    beta: Beta = field(default_factory=Beta)
    status: str = "open"
    parent: str | None = None
    children: list = field(default_factory=list)
    observations: int = 0
    epochs_without_evidence: int = 0
    falsified: bool = False

    def __post_init__(self):
        # Coerce enums if passed
        if isinstance(self.claim_kind, ClaimKind):
            self.claim_kind = self.claim_kind.value
        if isinstance(self.status, NodeStatus):
            self.status = self.status.value
        if isinstance(self.beta, dict):
            self.beta = Beta(**self.beta)

        # Validate
        if self.claim_kind not in CLAIM_KINDS:
            raise HypothesisError(f"Invalid claim_kind: {self.claim_kind!r}")
        if self.tag not in TAGS:
            raise HypothesisError(f"Invalid tag: {self.tag!r}")
        if self.status not in NODE_STATUSES:
            raise HypothesisError(f"Invalid status: {self.status!r}")

# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------

def posterior_mean(node_or_beta) -> float:
    """
    Beta posterior mean = alpha/(alpha+beta).
    Accepts a HypothesisNode (uses node.beta), a Beta, or a dict with alpha/beta.
    Returns 0.0 when alpha+beta <= 0.
    """
    if isinstance(node_or_beta, HypothesisNode):
        beta = node_or_beta.beta
    elif isinstance(node_or_beta, Beta):
        beta = node_or_beta
    elif isinstance(node_or_beta, dict):
        beta = Beta(**node_or_beta)
    else:
        raise TypeError("Expected HypothesisNode, Beta, or dict")
    return beta.mean()

def apply_evidence(node: HypothesisNode, supporting: bool, weight: float = DEFAULT_WEIGHT) -> HypothesisNode:
    """
    Apply evidence: update Beta parameters and increment observation count.
    On a supporting event: node.beta.alpha += weight. On a contradicting event: node.beta.beta += weight.
    ALWAYS increments node.observations by 1 (a weight-INDEPENDENT count). Does NOT touch the
    epoch/dormancy counter (that is the caller's per-epoch bookkeeping via advance_epoch).
    Mutates node in place and returns it.
    """
    if supporting:
        node.beta.alpha += weight
    else:
        node.beta.beta += weight
    node.observations += 1
    return refresh_status(node)

def evaluate_falsifiers(node: HypothesisNode, evidence: Any) -> bool:
    """
    Return True iff ANY predicate in node.falsifiers returns truthy for evidence.
    Predicates that raise are treated as not firing. Pure – no mutation.
    """
    for pred in node.falsifiers:
        try:
            if pred(evidence):
                return True
        except Exception:
            continue
    return False

def apply_falsifier_check(node: HypothesisNode, evidence: Any) -> HypothesisNode:
    """
    If evaluate_falsifiers returns True, latch node.falsified = True, then refresh_status.
    Once falsified the node stays refuted (a falsifier overrides Beta / a high posterior).
    Mutates node in place and returns it.
    """
    if evaluate_falsifiers(node, evidence):
        node.falsified = True
    return refresh_status(node)

def advance_epoch(node: HypothesisNode, new_evidence: int = 0) -> HypothesisNode:
    """
    Epoch-boundary bookkeeping for dormancy.
    If new_evidence == 0, increment epochs_without_evidence; else reset to 0.
    Then refresh_status. Mutates node and returns it.
    """
    if new_evidence == 0:
        node.epochs_without_evidence += 1
    else:
        node.epochs_without_evidence = 0
    return refresh_status(node)

def compute_status(node: HypothesisNode) -> str:
    """
    Pure derivation of node's status from its state (does NOT mutate).
    Precedence (SPEC §7.2):
      1. refuted  if node.falsified OR posterior_mean <= REFUTED_POSTERIOR_MAX
      2. supported if posterior_mean >= SUPPORTED_POSTERIOR_MIN AND
                     node.observations >= SUPPORTED_MIN_OBSERVATIONS
      3. dormant  if node.epochs_without_evidence >= DORMANT_EPOCHS
      4. open     otherwise
    Returns a STATUS_* string.
    """
    pm = posterior_mean(node)
    if node.falsified or pm <= REFUTED_POSTERIOR_MAX:
        return STATUS_REFUTED
    if pm >= SUPPORTED_POSTERIOR_MIN and node.observations >= SUPPORTED_MIN_OBSERVATIONS:
        return STATUS_SUPPORTED
    if node.epochs_without_evidence >= DORMANT_EPOCHS:
        return STATUS_DORMANT
    return STATUS_OPEN

def refresh_status(node: HypothesisNode) -> HypothesisNode:
    """Set node.status to compute_status(node) and return node."""
    node.status = compute_status(node)
    return node

def make_node(**kwargs) -> HypothesisNode:
    """Convenience constructor: create HypothesisNode from kwargs, then refresh_status."""
    node = HypothesisNode(**kwargs)
    return refresh_status(node)

# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------
__all__ = [
    "HypothesisError",
    "Beta",
    "HypothesisNode",
    "ClaimKind",
    "NodeStatus",
    "CLAIM_KINDS",
    "NODE_STATUSES",
    "TAGS",
    "STATUS_OPEN",
    "STATUS_SUPPORTED",
    "STATUS_REFUTED",
    "STATUS_DORMANT",
    "SUPPORTED_POSTERIOR_MIN",
    "SUPPORTED_MIN_OBSERVATIONS",
    "REFUTED_POSTERIOR_MAX",
    "DORMANT_EPOCHS",
    "DEFAULT_WEIGHT",
    "posterior_mean",
    "apply_evidence",
    "evaluate_falsifiers",
    "apply_falsifier_check",
    "advance_epoch",
    "compute_status",
    "refresh_status",
    "make_node",
]
