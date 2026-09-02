"""Test-pipe — named-config sweep runner (SPEC §8, §9 unit 9)."""
from __future__ import annotations

from core.testpipe.fixtures import fixture_items
from core.testpipe.sweep import build_named_configs, run_sweep
from core.testpipe.types import (
    AUDIT_OFF,
    FRONT_MODEL,
    FRONT_PASSTHROUGH,
    SPINE_COUNCIL,
    ItemResult,
)


def _configs(**kw):
    return build_named_configs(
        worker_W="local", worker_S="deepseek_v4_flash",
        audit_W="local", audit_S="minimax", front_backend="deepseek_v4_flash", **kw,
    )


def test_named_configs_are_ofat_around_the_anchor():
    names = [c.name for c in _configs()]
    assert names[0] == "B0"
    assert set(names) == {
        "B0", "front_MF", "repair_N", "audit_W", "audit_S", "worker_S",
        "stacked_all_weak", "stacked_hosted_max",
    }
    by = {c.name: c for c in _configs()}
    # anchor B0 = build / PT / worker=W / audit=off / repair=0 (bare-W floor, §8)
    assert by["B0"].front == FRONT_PASSTHROUGH and by["B0"].audit == AUDIT_OFF
    assert by["B0"].repair == 0 and by["B0"].worker == "local"
    # OFAT: each named variant moves exactly the one axis
    assert by["front_MF"].front == FRONT_MODEL and by["front_MF"].audit == AUDIT_OFF
    assert by["audit_W"].audit == "local" and by["audit_S"].audit == "minimax"
    assert by["worker_S"].worker == "deepseek_v4_flash"
    assert by["stacked_hosted_max"].front == FRONT_MODEL
    assert by["stacked_hosted_max"].worker == "deepseek_v4_flash"


def test_include_council_adds_the_shape_sweep():
    names = [c.name for c in _configs(include_council=True)]
    assert {"council_fusion", "council_sequential", "council_debate"} <= set(names)
    by = {c.name: c for c in _configs(include_council=True)}
    assert by["council_debate"].spine == SPINE_COUNCIL and by["council_debate"].shape == "debate"


async def test_run_sweep_aggregates_cost_and_pass(monkeypatch):
    items = fixture_items()
    configs = _configs()

    async def run_one(cfg, item):
        # synthetic: audit configs "pass" everything; B0 passes none — clean deltas
        passes = cfg.audit != AUDIT_OFF or cfg.repair > 0
        return ItemResult(item_id=item.id, config_name=cfg.name,
                          hidden_pass=passes, visible_pass=passes,
                          cost_usd=0.02, tokens=150)

    ledger = await run_sweep(configs, items, run_one)
    b0 = ledger.get("B0")
    assert b0.n_items == len(items) and b0.n_hidden_pass == 0
    assert round(b0.cost_usd, 6) == round(0.02 * len(items), 6)
    assert b0.tokens == 150 * len(items)
    assert ledger.get("audit_W").pass_at_1 == 1.0
    assert ledger.get("audit_W").slots["audit"] == "local"
