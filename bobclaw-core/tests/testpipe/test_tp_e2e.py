"""Test-pipe — network-free plumbing E2E (SPEC MS5-P1 §3.5).

Drives the fixture benchmark THROUGH the whole scaffold with MOCKED backends:
loader → split → chunk → (front → worker → audit → repair on the build spine) →
scorer-bridge → aggregate → ledger, plus the fence guard (SPEC §7.3) and the
chunk-resume identical-aggregate proof (SPEC §10). NO live model calls; the verify
gate + hidden scorer run the REAL subprocess sandbox on the tiny fixtures.
"""
from __future__ import annotations

import re

import core.config as cfg
import pytest

from core.testpipe.chunker import ChunkLedger, aggregate, run_chunk, split_chunks
from core.testpipe.fence import PromptRecorder
from core.testpipe.fixtures import fixture_items
from core.testpipe.ledger import Ledger
from core.testpipe.pipeline import run_item
from core.testpipe.scorer import SubprocessScorer
from core.testpipe.sweep import run_sweep
from core.testpipe.types import AUDIT_OFF, ConfigResult, SlotConfig


@pytest.fixture(autouse=True)
def _force_subprocess(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "subprocess")   # Docker-free, deterministic


def _canon_send(items):
    """Mocked worker: return the item's canonical solution (parsed by entry point)."""
    canon = {i.entry_point: i.canonical_solution for i in items}

    async def raw(messages, backend, model=None):
        m = re.search(r"def (\w+)\(", messages[-1]["content"])
        return canon.get(m.group(1), "") if m else ""
    return raw


async def test_full_plumbing_e2e_with_fence_guard(tmp_path):
    items = fixture_items()
    raw = _canon_send(items)
    scorer = SubprocessScorer(tmp_path / "score")
    config = SlotConfig(name="B0", worker="deepseek_v4_flash", audit=AUDIT_OFF, repair=0)
    rec = PromptRecorder()

    results = []
    for it in items:
        r = await run_item(it, config, raw_send=raw, scorer=scorer,
                          workspace_root=tmp_path / "ws", recorder=rec)
        results.append(r)

    # loader→split→spine→scorer all green on the fixtures (canonical impls)
    assert all(r.visible_pass and r.hidden_pass for r in results)
    # cost/token capture happened per item (SPEC §8)
    assert all(r.cost_usd > 0 and r.tokens > 0 for r in results)
    # ── FENCE GUARD (SPEC §7.3, HARD): no hidden payload in ANY recorded prompt ──
    for it in items:
        assert rec.leaks_for(it) == [], f"hidden leak for {it.id}"
    # and a positive: the recorder DID capture worker prompts (the guard isn't vacuous)
    assert any(p["stage"] == "worker" for p in rec.prompts)


async def test_audit_repair_e2e_recovers_a_wrong_first_impl(tmp_path):
    # front(model) → worker(wrong) → audit → repair(correct) → verify(green) → hidden pass.
    it = fixture_items()[0]  # fx_add

    async def raw(messages, backend, model=None):
        c = messages[-1]["content"]
        if "planning an implementation" in c:      # front model pass
            return "return a + b"
        if "You are reviewing" in c:               # audit stage
            return "the body returns 0; should add a and b"
        if "failed visible checks" in c:           # repair
            return "def add(a, b):\n    return a + b"
        return "def add(a, b):\n    return 0"      # worker: wrong first
    scorer = SubprocessScorer(tmp_path / "score")
    config = SlotConfig(name="stacked", front="model", front_backend="deepseek_v4_flash",
                        worker="deepseek_v4_flash", audit="minimax", repair=1)
    rec = PromptRecorder()
    r = await run_item(it, config, raw_send=raw, scorer=scorer,
                       workspace_root=tmp_path / "ws", recorder=rec)
    assert r.visible_pass and r.hidden_pass and r.rounds == 1
    stages = [p["stage"] for p in rec.prompts]
    assert stages == ["front", "worker", "audit", "repair"]   # full build spine ran
    assert rec.leaks_for(it) == []                            # fence held across all stages


async def test_chunk_resume_identical_aggregate_through_pipe(tmp_path):
    items = fixture_items()
    raw = _canon_send(items)
    config = SlotConfig(name="B0", worker="deepseek_v4_flash", audit=AUDIT_OFF, repair=0)
    buckets = split_chunks(items, 2)

    def make_score_one(root):
        async def score_one(item):
            scorer = SubprocessScorer(root / "score")
            return await run_item(item, config, raw_send=raw, scorer=scorer,
                                  workspace_root=root / "ws")
        return score_one

    # ── full uninterrupted run ──
    full_lgs = [ChunkLedger(tmp_path / f"full_{i}.jsonl") for i in range(2)]
    for lg, b in zip(full_lgs, buckets):
        await run_chunk(b, lg, make_score_one(tmp_path / "full"))
    full = aggregate(full_lgs)

    # ── interrupted (limit=1/chunk) then resumed ──
    lgs = [ChunkLedger(tmp_path / f"r_{i}.jsonl") for i in range(2)]
    for lg, b in zip(lgs, buckets):
        await run_chunk(b, lg, make_score_one(tmp_path / "r"), limit=1)
    for lg, b in zip(lgs, buckets):
        await run_chunk(b, lg, make_score_one(tmp_path / "r"))
    resume = aggregate(lgs)

    assert full.n_items == len(items) and full.n_hidden_pass == len(items)  # all canonical → pass
    assert resume.fingerprint() == full.fingerprint()          # SPEC §10 identical aggregate
    assert resume.n_hidden_pass == full.n_hidden_pass


async def test_sweep_to_ledger_report_end_to_end(tmp_path):
    # The full measurement path: named configs → run_sweep → ledger.report (synthetic
    # per-config results — the sweep STRUCTURE, no live runs, SPEC §8/§10).
    items = fixture_items()
    configs = [
        SlotConfig(name="B0", worker="local", audit=AUDIT_OFF, repair=0),
        SlotConfig(name="audit_S", worker="local", audit="minimax", repair=1),
    ]

    async def run_one(cfg_, item):
        from core.testpipe.types import ItemResult
        passed = cfg_.audit != AUDIT_OFF
        return ItemResult(item_id=item.id, config_name=cfg_.name,
                          hidden_pass=passed, visible_pass=passed,
                          cost_usd=0.05 if passed else 0.01, tokens=200)

    ledger = await run_sweep(configs, items, run_one)
    triple = ledger.uplift_triple("B0", "audit_S", bare_at_n_oracle=1.0)
    report = ledger.report(deltas=[("audit", "B0", "audit_S")], triple=triple)
    assert triple.bare_at_1 == 0.0 and triple.bob_final == 1.0
    d = report["slot_deltas"][0]
    assert d["delta_pass"] == 1.0 and d["variant_provenance"]["audit"] == "hosted"
    by = {c["name"]: c for c in report["configs"]}
    assert by["audit_S"]["all_local"] is False        # hosted audit labelled honestly (§7.4)
