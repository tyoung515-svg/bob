"""MS9-FE2E — the ``core/forest/`` INTEGRATION test: bobpipe seed program, weeks-in-minutes.

Drives the FULL research-forest cycle (SPEC-RESEARCH-FOREST §5/§2) end-to-end through the shared
driver ``tasks/2026-07-07-mega-sprint-9/e2e/bobpipe_cycle.py`` and asserts EACH of the eight stages'
outcomes:

  0. create the bobpipe program (F1)
  1. synthetic multi-"week" backfill via observers ticking (F3)
  2. the delta gate fires at the §7.3 threshold (≥ 10 subtree evidence events)
  3. ONE hypothesize epoch, injected MOCK arm — NOT live (F4): verified claims append, halt + budget
  4. observational A/B race → 2-parent merge=synthesis judgment recorded (F7)
  5. measurement-entailment tags land (F5): PV where entailed, U below the observation floor (default-FAIL)
  6. fork PROPOSAL artifact emitted — proposal-only, nothing auto-applies (F7, inv. 14)
  7. projection rebuilt + freshness green (F8) — HERMETIC: a faithful in-memory FakeQdrant double +
     the deterministic fixture embedder (``pytest.ini`` runs ``--disable-socket``; the LIVE round-trip
     against Qdrant :6353 is exercised by the runnable harness ``run_bobpipe_e2e.py``, not here)
  8. epoch digest artifact rendered by REUSING W2's ``core/watch/digest.py``

**Honest scope (inv. 13).** Synthetic backfill + the F4 mocked arm — deterministic + offline. The one
sanctioned micro LIVE testpipe-uplift run was SPENT by F6 (``sprints/RESULTS-F6.md``); FE2E does not
repeat it. The cycle is run ONCE (module-scoped fixture — git-backed ledger commits are the slow part)
and the stages are asserted independently.
"""
from __future__ import annotations

import asyncio
import math
import pathlib
import sys

import pytest

from core.forest.program import ForestRegistry
from core.forest.projection import ForestProjection, deterministic_embedder

# Import the shared cycle driver from the sprint's e2e harness dir (a top-level module, unique name).
_E2E_DIR = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "2026-07-07-mega-sprint-9" / "e2e"
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))
import bobpipe_cycle  # noqa: E402


# ---------------------------------------------------------------------------
# Hermetic in-memory Qdrant double — interprets the REAL qdrant model objects the projection module
# builds (Filter/FieldCondition/MatchValue/MatchAny/FilterSelector/PointStruct/VectorParams), so
# projection.py runs its exact production client-call path (mirrors tests/forest/test_forest_projection.py).
# ---------------------------------------------------------------------------
class _Pt:
    def __init__(self, pid, score, payload):
        self.id, self.score, self.payload = pid, score, payload


class _Resp:
    def __init__(self, points):
        self.points = points


def _match_cond(cond, payload) -> bool:
    key, m = cond.key, cond.match
    if hasattr(m, "value") and m.value is not None:
        return payload.get(key) == m.value
    if hasattr(m, "any") and m.any is not None:
        return payload.get(key) in list(m.any)
    return False


def _match_filter(flt, payload) -> bool:
    if flt is None:
        return True
    for cond in (getattr(flt, "must", None) or []):
        if not _match_cond(cond, payload):
            return False
    return True


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


class FakeQdrant:
    """Minimal, faithful in-memory Qdrant: only the calls projection.py makes, on real model objects."""

    def __init__(self):
        self._colls: dict = {}

    def collection_exists(self, collection_name) -> bool:
        return collection_name in self._colls

    def scroll(self, collection_name, scroll_filter=None, limit=10, with_payload=True,
               with_vectors=False, offset=None):
        coll = self._colls.get(collection_name)
        if coll is None:
            return [], None
        pts = [_Pt(pid, 0.0, rec["payload"]) for pid, rec in coll["points"].items()
               if _match_filter(scroll_filter, rec["payload"])]
        return pts[:limit], None

    def query_points(self, collection_name, query, limit=10, query_filter=None):
        coll = self._colls.get(collection_name)
        if coll is None:
            raise RuntimeError(f"collection {collection_name!r} not found")
        scored = [_Pt(pid, _cosine(query, rec["vector"]), rec["payload"])
                  for pid, rec in coll["points"].items()
                  if _match_filter(query_filter, rec["payload"])]
        scored.sort(key=lambda p: p.score, reverse=True)
        return _Resp(scored[:limit])

    def create_collection(self, collection_name, vectors_config):
        self._colls[collection_name] = {"dim": vectors_config.size, "points": {}}

    def delete_collection(self, collection_name):
        self._colls.pop(collection_name, None)

    def upsert(self, collection_name, points):
        coll = self._colls[collection_name]
        for p in points:
            coll["points"][p.id] = {"vector": list(p.vector), "payload": dict(p.payload)}

    def delete(self, collection_name, points_selector):
        coll = self._colls.get(collection_name)
        if coll is None:
            return
        flt = getattr(points_selector, "filter", None)
        for pid in [pid for pid, rec in coll["points"].items() if _match_filter(flt, rec["payload"])]:
            del coll["points"][pid]


# ---------------------------------------------------------------------------
# Run the whole cycle ONCE (module-scoped — the git-backed ledger commits are the slow part).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cycle(tmp_path_factory):
    root = tmp_path_factory.mktemp("fe2e")
    registry = ForestRegistry(root=root / "forest")
    proj = ForestProjection(FakeQdrant(), embedder=deterministic_embedder(64),
                            collection="research_forest__test_fe2e_hermetic")
    result = asyncio.run(bobpipe_cycle.run_cycle(
        registry=registry,
        proj=proj,
        program_id="bobpipe-uplift",
        artifact_dir=root / "artifacts",
        meta_ledger_path=root / "bobpipe__meta.jsonl",
    ))
    return result


# ---------------------------------------------------------------------------
# Stage-by-stage assertions
# ---------------------------------------------------------------------------
def test_stage0_program_created(cycle):
    assert cycle.program_id == "bobpipe-uplift"
    assert cycle.standing_question == bobpipe_cycle.STANDING_QUESTION
    assert cycle.base_ref  # a resolvable pre-backfill ref


def test_stage1_backfill_lands_multi_week_history(cycle):
    # 6 weekly uplift + 5 weekly pass@1 + 2 latency = 13 measurement events
    assert cycle.n_backfilled == 13


def test_stage2_delta_gate_fires_at_threshold(cycle):
    assert cycle.delta_threshold == 10
    assert cycle.delta_count == 13
    assert cycle.delta_fired is True


def test_stage3_epoch_runs_mocked_halts_and_respects_budget(cycle):
    assert cycle.epoch_blocked is False
    assert cycle.epoch_halted is True                 # halts after 2 dry passes (SPEC §7.3)
    assert len(cycle.epoch_verified_ids) == 2         # the mocked arm's two verified claims landed
    assert cycle.epoch_total_est_cost < cycle.budget_cap
    assert cycle.budget_cap == 3.0
    assert cycle.budget_cap_binds is True             # an at-cap pass is blocked


def test_stage4_ab_race_records_two_parent_merge(cycle):
    assert cycle.ab_judged is True
    assert cycle.ab_winner == "armX"                  # audit=on posterior wins
    assert cycle.ab_merge_sha                          # a real 2-parent merge=synthesis commit
    assert cycle.ab_delta >= 0.2                       # posteriors separated


def test_stage5_entailment_tags_pv_and_default_fail_u(cycle):
    assert cycle.tags["H1"] == "PV"                   # uplift trend, entailed, primary → PV
    assert cycle.tags["H2"] == "PV"                   # pass@1 level in band → PV
    assert cycle.tags["H3"] == "U"                    # only 2 obs < floor 5 → default-FAIL → U
    assert cycle.entailments["H3"].floor_met is False
    # PV is structurally unreachable below the floor
    assert cycle.entailments["H3"].n_observations == 2


def test_stage6_fork_proposal_is_proposal_only(cycle):
    assert cycle.fork_proposal is not None
    assert cycle.fork_proposal.child_program_id == "bobpipe-audit-slot-uplift"
    assert cycle.fork_proposal.rationale                     # the mandatory "why it can't stay a subtree"
    assert cycle.fork_proposal.seed_key.startswith("forkseed:sha256:")
    assert pathlib.Path(cycle.fork_artifact_path).is_file()  # the artifact was written
    assert cycle.fork_auto_applied is False                  # inv. 14: nothing auto-applied


def test_stage7_projection_rebuild_and_freshness(cycle):
    assert cycle.projection_count > 0
    assert cycle.retrieve_hits > 0
    assert cycle.fresh_after_rebuild is True
    assert cycle.stale_after_advance is True
    assert cycle.fresh_after_second_rebuild is True


def test_stage8_digest_artifact_via_w2(cycle):
    assert cycle.digest_id
    assert cycle.digest_markdown.startswith("# bobpipe uplift — epoch digest")
    assert "## Alerts" in cycle.digest_markdown              # the fork-proposal alert fired
    assert cycle.digest_doc["kind"] == "watch_digest"
    assert cycle.digest_doc["digest_id"] == cycle.digest_id


def test_all_nine_stages_ok(cycle):
    """The whole cycle wired and every stage reported ok (0..8 = 9 stages)."""
    assert bobpipe_cycle.all_stages_ok(cycle) is True
    assert [s.n for s in cycle.stages] == list(range(9))
