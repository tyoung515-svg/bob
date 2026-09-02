"""Test-pipe — results ledger + report (SPEC §9 unit 10, §10 acceptance).

The measurement payload of the whole pipe (SPEC §0: measure the SYSTEM, attribute the
uplift to slots). Given the per-config aggregates (SPEC §8), this computes the DATA
STRUCTURES SPEC §10 requires:

  * **uplift triple** — ``bare@1 / bare@N-oracle / BoB-final`` (SPEC §10).
  * **per-slot deltas** — vary ONE axis (front PT↔MF, repair 0→N, audit off→W→S, worker
    W→S; council shape fusion→sequential→debate), each a pass@1 + cost delta (SPEC §8).
  * **uplift-per-dollar** — the real BoB thesis number (SPEC §0 secondary metric).
  * **slot provenance** — local vs hosted per config, so a hosted-audit gain is never
    reported "all-local" (SPEC §7.4).

Pure + synthetic-testable: it consumes :class:`ConfigResult`s, so the ledger is proven
with SYNTHETIC per-config results (no live runs — SPEC MS5-P1 scope).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from core.testpipe.types import ConfigResult


def uplift_per_dollar(delta_pass: float, delta_cost_usd: float) -> float:
    """pass@1 gained per USD spent (SPEC §0). ``delta_cost<=0`` ⇒ 0.0 (a free/cheaper
    gain is reported as its pass delta elsewhere; per-dollar needs a positive cost)."""
    return round(delta_pass / delta_cost_usd, 6) if delta_cost_usd > 0 else 0.0


@dataclass
class UpliftTriple:
    """SPEC §10: the headline three numbers."""

    bare_at_1: float
    bare_at_n_oracle: float
    bob_final: float

    @property
    def bob_over_bare(self) -> float:
        return round(self.bob_final - self.bare_at_1, 6)

    @property
    def headroom_captured(self) -> float:
        """Fraction of the bare@1→oracle headroom BoB captured (0 if no headroom)."""
        head = self.bare_at_n_oracle - self.bare_at_1
        return round((self.bob_final - self.bare_at_1) / head, 6) if head > 0 else 0.0


@dataclass
class SlotDelta:
    """The measured value of ONE slot change (SPEC §8 OFAT attribution)."""

    axis: str
    anchor: str
    variant: str
    delta_pass: float
    delta_cost_usd: float
    uplift_per_dollar: float
    anchor_provenance: dict = field(default_factory=dict)
    variant_provenance: dict = field(default_factory=dict)


class Ledger:
    """Accumulates per-config aggregates + computes the SPEC §10 attribution."""

    def __init__(self) -> None:
        self.configs: dict[str, ConfigResult] = {}

    def add(self, cr: ConfigResult) -> None:
        self.configs[cr.name] = cr

    def get(self, name: str) -> ConfigResult:
        if name not in self.configs:
            raise KeyError(f"no config {name!r} in ledger; have {sorted(self.configs)}")
        return self.configs[name]

    def slot_delta(self, axis: str, anchor: str, variant: str) -> SlotDelta:
        """The pass@1 + cost delta of moving ONE axis from *anchor* to *variant*."""
        a, v = self.get(anchor), self.get(variant)
        dpass = round(v.pass_at_1 - a.pass_at_1, 6)
        dcost = round(v.cost_usd - a.cost_usd, 6)
        return SlotDelta(
            axis=axis, anchor=anchor, variant=variant,
            delta_pass=dpass, delta_cost_usd=dcost,
            uplift_per_dollar=uplift_per_dollar(dpass, dcost),
            anchor_provenance=dict(a.provenance), variant_provenance=dict(v.provenance),
        )

    def uplift_triple(
        self, bare: str, bob: str, *, bare_at_n_oracle: float
    ) -> UpliftTriple:
        """SPEC §10 triple: bare@1 (the B0 anchor's pass@1), the provided bare@N-oracle
        reference ceiling, and BoB-final (the full-system config's pass@1)."""
        return UpliftTriple(
            bare_at_1=round(self.get(bare).pass_at_1, 6),
            bare_at_n_oracle=round(bare_at_n_oracle, 6),
            bob_final=round(self.get(bob).pass_at_1, 6),
        )

    def report(
        self,
        *,
        deltas: Optional[list] = None,
        triple: Optional[UpliftTriple] = None,
    ) -> dict:
        """A serializable report: per-config metrics + provenance + the requested
        deltas + triple (SPEC §10). ``deltas`` is a list of ``(axis, anchor, variant)``
        tuples the caller wants attributed."""
        computed = [self.slot_delta(*d) for d in (deltas or [])]
        return {
            "configs": [
                {
                    "name": c.name, "n_items": c.n_items,
                    "pass_at_1": round(c.pass_at_1, 6),
                    "visible_rate": round(c.visible_rate, 6),
                    "cost_usd": round(c.cost_usd, 6), "tokens": c.tokens,
                    "provenance": dict(c.provenance), "slots": dict(c.slots),
                    "all_local": all(
                        p == "local" for k, p in c.provenance.items() if p not in ("n/a",)
                    ) if c.provenance else None,
                }
                for c in self.configs.values()
            ],
            "slot_deltas": [asdict(d) for d in computed],
            "uplift_triple": asdict(triple) if triple else None,
            "uplift_triple_derived": (
                {"bob_over_bare": triple.bob_over_bare,
                 "headroom_captured": triple.headroom_captured}
                if triple else None
            ),
        }
