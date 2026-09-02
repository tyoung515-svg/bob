"""Test-pipe — core data types (SPEC §9 units 1, 3, 8, 9, 10).

Pure dataclasses shared by every stage of the pipe. No network, no LLM, no
filesystem — importing this module is side-effect-free. The two spines (SPEC §2)
and the four swept slots (SPEC §3) are all modelled here as DATA so the sweep
runner (§8) can name a config and the ledger (§10) can attribute an uplift to a
single slot.

Honesty note carried from SPEC §7.4: :class:`SlotConfig` records ``provenance``
(local vs hosted) per config so a hosted-audit gain is never mislabelled
"all-local" in the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

# ── slot-label sentinels ────────────────────────────────────────────────────
# ``front`` axis (SPEC §3): both variants are ALWAYS measured; the PT↔MF delta is
# the measured value of the plan pass.
FRONT_PASSTHROUGH = "passthrough"
FRONT_MODEL = "model"

# ``audit`` slot (SPEC §3/§4): off | a backend string. "off" ⇒ the build loop is
# byte-identical to today (no LLM critic in the loop).
AUDIT_OFF = "off"

# the two spines a benchmark plugin can declare (SPEC §2).
SPINE_BUILD = "build"
SPINE_COUNCIL = "council"

# Backends that run on the user's own hardware (SPEC §7.4 provenance labelling).
# A conservative allowlist — everything else is treated as hosted so a config can
# never over-claim "all-local". Kept here (not config) so the label is a pure,
# test-pinnable function of the slot, independent of live backend availability.
LOCAL_BACKENDS: frozenset[str] = frozenset({"local", "lmstudio", "ollama"})


def provenance_of(backend: str) -> str:
    """"local" iff *backend* runs on-box, else "hosted" (SPEC §7.4).

    ``off``/empty ⇒ "local" (no hosted call is made for that slot)."""
    b = (backend or "").strip()
    if not b or b == AUDIT_OFF:
        return "local"
    return "local" if b in LOCAL_BACKENDS else "hosted"


@dataclass(frozen=True)
class TestItem:
    """One benchmark problem (SPEC §9 unit 1).

    ``prompt`` is the function header + docstring the pipe SHOWS a model.
    ``visible_tests`` (docstring ``>>>`` doctests / examples — SPEC §7.1) are the
    ONLY assertions that ever flow to front/worker/audit/repair/verify.
    ``hidden_tests`` are held by the external scorer (SPEC §7.2) and MUST never
    enter a model prompt (the fence, SPEC §7.3). Each test is a full Python
    assertion string referencing ``entry_point`` (e.g. ``"assert add(1, 2) == 3"``).
    """

    # Not a pytest test class despite the ``Test`` prefix (it's a benchmark item).
    __test__ = False

    id: str
    prompt: str
    entry_point: str
    visible_tests: tuple[str, ...] = ()
    hidden_tests: tuple[str, ...] = ()
    # Optional extras (additive to the SPEC's 5-field shape): a parsed signature
    # for the build spine's skeleton, and a reference solution used ONLY by the
    # bare-worker reference ceiling (SPEC §8) — never shown to the pipe.
    signature: str = ""
    canonical_solution: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_tests", tuple(self.visible_tests))
        object.__setattr__(self, "hidden_tests", tuple(self.hidden_tests))


@dataclass(frozen=True)
class SlotConfig:
    """A named point in the ``spine × front × worker × audit × repair × shape``
    space (SPEC §8). Slots are bare backend strings (SPEC §3 — reuse ``teams``,
    no new slotting)."""

    name: str
    spine: str = SPINE_BUILD
    front: str = FRONT_PASSTHROUGH          # passthrough | model
    front_backend: str = "local"            # backend for the model front pass
    worker: str = "local"                   # worker backend slot
    audit: str = AUDIT_OFF                  # off | backend
    repair: int = 0                         # repair rounds (0 = verify once)
    shape: Optional[str] = None             # council spine only: fusion|sequential|debate

    def with_name(self, name: str) -> "SlotConfig":
        return replace(self, name=name)

    def provenance(self) -> dict:
        """Per-slot local/hosted labels (SPEC §7.4). ``front`` is labelled only
        when the model pass is active (passthrough makes no model call)."""
        return {
            "worker": provenance_of(self.worker),
            "audit": provenance_of(self.audit),
            "front": provenance_of(self.front_backend)
            if self.front == FRONT_MODEL else "local",
            "shape": provenance_of(self.shape or "") if self.spine == SPINE_COUNCIL else "n/a",
        }

    def all_local(self) -> bool:
        """True iff every ACTIVE slot is local (honest 'all-local' gate)."""
        prov = self.provenance()
        return all(v == "local" for k, v in prov.items() if v not in ("n/a",))


@dataclass
class ItemResult:
    """The pipe's outcome for ONE item under ONE config (before hidden scoring is
    folded in). ``visible_pass`` = the gate the pipe optimised; ``hidden_pass`` =
    the external scorer's contamination-proof verdict (SPEC §7.2, the primary
    metric ``pass@1-of-final``)."""

    item_id: str
    config_name: str
    impl: Optional[str] = None
    visible_pass: bool = False
    hidden_pass: bool = False
    rounds: int = 0                         # repair rounds actually run
    audit_findings: tuple[str, ...] = ()
    cost_usd: float = 0.0
    tokens: int = 0
    provenance: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["audit_findings"] = list(self.audit_findings)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ItemResult":
        d = dict(d)
        d["audit_findings"] = tuple(d.get("audit_findings") or ())
        return cls(**{k: v for k, v in d.items() if k in cls.__annotations__})


@dataclass
class ConfigResult:
    """Aggregate of one named config over the item set (SPEC §9 unit 9)."""

    name: str
    n_items: int = 0
    n_hidden_pass: int = 0
    n_visible_pass: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    provenance: dict = field(default_factory=dict)
    slots: dict = field(default_factory=dict)

    @property
    def pass_at_1(self) -> float:
        """Primary metric: pass@1-of-final on HIDDEN tests (SPEC §0)."""
        return (self.n_hidden_pass / self.n_items) if self.n_items else 0.0

    @property
    def visible_rate(self) -> float:
        return (self.n_visible_pass / self.n_items) if self.n_items else 0.0
