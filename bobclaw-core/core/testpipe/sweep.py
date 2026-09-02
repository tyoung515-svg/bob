"""Test-pipe — named-config sweep runner (SPEC §8, §9 unit 9).

Turns the OFAT + stacked NAMED configs (SPEC §8 — NOT a cartesian grid) into runs,
capturing cost + tokens per config into a :class:`~core.testpipe.ledger.Ledger`.
Network-free: ``run_one(config, item) -> ItemResult`` is injected, so the sweep
STRUCTURE (named configs → aggregate → ledger) is proven with synthetic per-item
results (no live runs, no spend — SPEC MS5-P1 scope).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Awaitable, Callable

from core.testpipe.ledger import Ledger
from core.testpipe.types import (
    AUDIT_OFF,
    FRONT_MODEL,
    FRONT_PASSTHROUGH,
    SPINE_BUILD,
    SPINE_COUNCIL,
    ConfigResult,
    ItemResult,
    SlotConfig,
    TestItem,
)

RunOne = Callable[[SlotConfig, TestItem], Awaitable[ItemResult]]


def _slots(cfg: SlotConfig) -> dict:
    return {"spine": cfg.spine, "front": cfg.front, "worker": cfg.worker,
            "audit": cfg.audit, "repair": cfg.repair, "shape": cfg.shape}


def build_named_configs(
    *,
    worker_W: str,
    worker_S: str,
    audit_W: str,
    audit_S: str,
    front_backend: str,
    repair_n: int = 1,
    include_council: bool = False,
) -> list[SlotConfig]:
    """The SPEC §8 named-config plan: anchor B0 + OFAT attribution + stacked systems.

    ~9–12 named configs (within §8's 12–16 target): a clean anchor, one-factor-at-a-
    time variants around it (front PT↔MF, repair 0→N, audit off→W→S, worker W→S), and
    the stacked all-weak / hosted-max systems. Optionally the council-spine shape sweep
    (fusion/sequential/debate)."""
    B0 = SlotConfig(name="B0", spine=SPINE_BUILD, front=FRONT_PASSTHROUGH,
                    front_backend=front_backend, worker=worker_W, audit=AUDIT_OFF, repair=0)
    configs = [
        B0,
        replace(B0, name="front_MF", front=FRONT_MODEL),
        replace(B0, name="repair_N", repair=repair_n),
        replace(B0, name="audit_W", audit=audit_W, repair=repair_n),
        replace(B0, name="audit_S", audit=audit_S, repair=repair_n),
        replace(B0, name="worker_S", worker=worker_S),
        # stacked / system (SPEC §8): all-weak W/W/N and hosted-max S/S/S.
        replace(B0, name="stacked_all_weak", audit=audit_W, repair=repair_n),
        replace(B0, name="stacked_hosted_max", front=FRONT_MODEL, worker=worker_S,
                audit=audit_S, repair=repair_n),
    ]
    if include_council:
        for shape in ("fusion", "sequential", "debate"):
            configs.append(SlotConfig(
                name=f"council_{shape}", spine=SPINE_COUNCIL, front=FRONT_PASSTHROUGH,
                front_backend=front_backend, worker=worker_W, audit=AUDIT_OFF,
                repair=0, shape=shape))
    return configs


async def run_sweep(
    configs: list[SlotConfig], items: list[TestItem], run_one: RunOne,
) -> Ledger:
    """Run every (config × item), aggregate per config, return the ledger (SPEC §8).

    Cost + tokens are summed per config from each item's :class:`ItemResult` (the
    real capture path — the injected ``run_one`` supplies them). One aggregate row per
    named config; the ledger then attributes per-slot deltas (SPEC §9 unit 10)."""
    ledger = Ledger()
    for cfg in configs:
        cr = ConfigResult(name=cfg.name, provenance=cfg.provenance(), slots=_slots(cfg))
        for item in items:
            r = await run_one(cfg, item)
            cr.n_items += 1
            cr.n_hidden_pass += int(bool(r.hidden_pass))
            cr.n_visible_pass += int(bool(r.visible_pass))
            cr.cost_usd += r.cost_usd
            cr.tokens += r.tokens
        cr.cost_usd = round(cr.cost_usd, 6)
        ledger.add(cr)
    return ledger
