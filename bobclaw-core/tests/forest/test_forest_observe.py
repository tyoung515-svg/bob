"""MS9-F3 — tests for core.forest.observe (observers + trigger verbs) and core.forest.sources.

Pins the F3 accept criteria:
  1. A fixture source -> `measurement` events land in the program ledger (round-trip via the F1
     store + read_ledger_at).
  2. The delta gate fires at EXACTLY the §7.3 threshold (>= 10 new evidence events for a subtree) and
     NOT below (9 -> no fire, 10 -> fire); subtree isolation; non-evidence events excluded.
  3. The scheduler binding is proven with an OFFLINE one-tick test (the P5 run_tick pattern — fixed
     clock, injected claim + invoke, no daemon, no network).
Plus: per-kind resolvers, the eval-results FILE reader, zero-value regression guard, idempotent
re-ticks, falsifier/manual verbs + dispatch, the recurrence envelope, and the thin sources registry.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.forest import events as fe
from core.forest.store import create_program
from core.forest.hypothesis import make_node
from core.forest.sources import (
    FOREST_SOURCE_KINDS,
    ForestSource,
    ForestSourcesRegistry,
)
from core.forest import observe
from core.forest.observe import (
    DELTA_THRESHOLD,
    ObserveError,
    ObserveResult,
    RESOLVERS,
    TriggerResult,
    count_new_evidence,
    dedupe_events,
    evaluate_delta,
    evaluate_falsifier,
    evaluate_trigger,
    load_fixture,
    manual_trigger,
    new_evidence_events,
    observer_profile,
    observer_task,
    parse_observer_task,
    resolve_source,
    run_observer,
)
from core.scheduler import build_schedule_seed, run_tick

UTC = timezone.utc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _src(kind="testpipe_uplift", *, id="uplift", node_id="H1", metric="uplift", **kw):
    return ForestSource(id=id, kind=kind, node_id=node_id, metric=metric, **kw)


def _append_measurements(store, n, *, node_id="H1", metric="uplift", start=0):
    """Append n DISTINCT measurement events (distinct value+ts -> distinct content id)."""
    evs = [
        fe.measurement(node_id=node_id, metric=metric, value=float(start + i),
                       source="testpipe", ts=1000 + start + i)
        for i in range(n)
    ]
    return store.append_events(evs)


# ---------------------------------------------------------------------------
# ACCEPT 1 — a fixture source -> measurement events land in the program ledger
# ---------------------------------------------------------------------------

def test_run_observer_lands_measurement_events(tmp_path):
    store = create_program("bobpipe", root=tmp_path)
    src = _src(node_id="H1", metric="uplift")
    payload = [{"value": 0.42}, {"value": 0.55}, {"value": 0.61}]
    res = run_observer(store, src, payload, ts=1000)

    assert isinstance(res, ObserveResult)
    assert res.sha is not None
    assert res.count == 3 and res.skipped == 0

    # round-trip via read_ledger_at (store.read == read_ledger_at)
    snap = store.read()
    landed = [e for e in snap["events"] if e["kind"] == "measurement"]
    assert len(landed) == 3
    for e in landed:
        assert e["node_id"] == "H1"
        assert e["metric"] == "uplift"
        assert e["source"] == "uplift"          # == source.id
    assert sorted(e["value"] for e in landed) == [0.42, 0.55, 0.61]


def test_resolver_registered_for_every_source_kind():
    assert set(RESOLVERS) == set(FOREST_SOURCE_KINDS)


def test_each_kind_resolves_to_measurements():
    # testpipe-uplift stub
    ev = resolve_source(_src("testpipe_uplift", metric="uplift"), {"value": 1.5}, ts=1)
    assert len(ev) == 1 and ev[0]["kind"] == "measurement" and ev[0]["value"] == 1.5

    # flight/spend read-only reader (accepts an "amount" reading)
    ev = resolve_source(_src("flight_spend", id="spend", metric="cost"),
                        [{"amount": 3.0}, {"amount_usd": 4.0}], ts=1)
    assert [e["value"] for e in ev] == [3.0, 4.0]
    assert all(e["metric"] == "cost" for e in ev)

    # eval-results wrapper
    ev = resolve_source(_src("eval_results", id="ses", metric="score"),
                        {"results": [{"value": 0.8}, {"metric": "f1", "value": 0.9}]}, ts=1)
    assert [e["value"] for e in ev] == [0.8, 0.9]
    assert [e["metric"] for e in ev] == ["score", "f1"]

    # lks corpus stats — a plain {metric: numeric} map
    ev = resolve_source(_src("lks_stats", id="lks", metric="unused"),
                        {"doc_count": 100, "avg_len": 42.5}, ts=1)
    got = {e["metric"]: e["value"] for e in ev}
    assert got == {"doc_count": 100.0, "avg_len": 42.5}


def test_eval_results_file_reader(tmp_path):
    # the "eval-results FILE reader": load_fixture + resolve
    p = tmp_path / "evals.json"
    p.write_text(json.dumps({"results": [{"value": 0.7}, {"value": 0.9}]}), encoding="utf-8")
    payload = load_fixture(p)
    ev = resolve_source(_src("eval_results", id="ses", metric="score"), payload, ts=5)
    assert [e["value"] for e in ev] == [0.7, 0.9]


def test_zero_value_measurement_is_kept():
    # regression guard: a falsy-but-valid value: 0 must NOT be dropped.
    ev = resolve_source(_src("testpipe_uplift", metric="uplift"), [{"value": 0}, {"value": 0.0}], ts=1)
    assert [e["value"] for e in ev] == [0.0, 0.0]


def test_reading_without_value_is_skipped_not_crashed():
    ev = resolve_source(_src("testpipe_uplift"), [{"note": "no value here"}, {"value": 2.0}], ts=1)
    assert [e["value"] for e in ev] == [2.0]


def test_resolve_source_unknown_kind_raises():
    bogus = SimpleNamespace(kind="not-a-kind", id="x", node_id="H1", metric="m", unit=None, weight=1)
    with pytest.raises(ObserveError):
        resolve_source(bogus, {"value": 1}, ts=1)


def test_run_observer_empty_payload_is_noop(tmp_path):
    store = create_program("p", root=tmp_path)
    res = run_observer(store, _src(), [], ts=1)
    assert res.sha is None and res.count == 0 and res.skipped == 0
    assert store.events() == []


# ---------------------------------------------------------------------------
# dedupe / idempotency
# ---------------------------------------------------------------------------

def test_run_observer_is_idempotent_on_retick(tmp_path):
    store = create_program("p", root=tmp_path)
    src = _src()
    payload = [{"value": 1.0}, {"value": 2.0}]
    r1 = run_observer(store, src, payload, ts=1)
    assert r1.count == 2
    # identical re-tick: every event id already in the ledger -> nothing appended.
    r2 = run_observer(store, src, payload, ts=1)
    assert r2.count == 0 and r2.skipped == 2 and r2.sha is None
    assert len([e for e in store.events() if e["kind"] == "measurement"]) == 2


def test_dedupe_events_collapses_seen_and_intrabatch():
    e1 = fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1)
    e2 = fe.measurement(node_id="H1", metric="m", value=2.0, source="s", ts=2)
    dup_e1 = fe.measurement(node_id="H1", metric="m", value=1.0, source="s", ts=1)  # same content/id
    kept, dropped = dedupe_events([e1, e2, dup_e1], seen_ids=set())
    assert [e["id"] for e in kept] == [e1["id"], e2["id"]]
    assert dropped == 1
    # a previously-seen id is dropped
    kept2, dropped2 = dedupe_events([e1, e2], seen_ids={e1["id"]})
    assert [e["id"] for e in kept2] == [e2["id"]] and dropped2 == 1


# ---------------------------------------------------------------------------
# ACCEPT 2 — delta gate fires at EXACTLY the §7.3 threshold
# ---------------------------------------------------------------------------

def test_delta_threshold_constant_is_ten():
    assert DELTA_THRESHOLD == 10


def test_delta_gate_boundary_nine_no_fire_ten_fires(tmp_path):
    store = create_program("p", root=tmp_path)
    base = store.head()

    _append_measurements(store, 9, node_id="H1", start=0)
    r9 = evaluate_delta(store, base)
    assert r9.verb == "delta" and r9.count == 9 and r9.fired is False   # 9 -> NO fire

    _append_measurements(store, 1, node_id="H1", start=9)              # now 10 total
    r10 = evaluate_delta(store, base)
    assert r10.count == 10 and r10.fired is True                        # 10 -> FIRE (exact threshold)

    _append_measurements(store, 1, node_id="H1", start=10)             # 11
    assert evaluate_delta(store, base).count == 11 and evaluate_delta(store, base).fired is True


def test_delta_gate_is_subtree_scoped(tmp_path):
    store = create_program("p", root=tmp_path)
    base = store.head()
    _append_measurements(store, 10, node_id="H1", start=0)
    _append_measurements(store, 3, node_id="H2", start=100)

    assert evaluate_delta(store, base, node_ids={"H1"}).count == 10   # H1 subtree -> fire
    assert evaluate_delta(store, base, node_ids={"H1"}).fired is True
    r_h2 = evaluate_delta(store, base, node_ids={"H2"})
    assert r_h2.count == 3 and r_h2.fired is False                    # H2 subtree -> below
    assert evaluate_delta(store, base).count == 13                    # whole program


def test_delta_gate_excludes_non_evidence_events(tmp_path):
    store = create_program("p", root=tmp_path)
    base = store.head()
    # 12 spend events (NOT evidence-bearing) + 5 measurements
    store.append_events([fe.spend(amount_usd=float(i), label="obs", ts=1) for i in range(12)])
    _append_measurements(store, 5, node_id="H1", start=0)
    r = evaluate_delta(store, base)
    assert r.count == 5 and r.fired is False   # only measurements count toward the gate


def test_delta_no_change_counts_zero(tmp_path):
    store = create_program("p", root=tmp_path)
    head = store.head()
    assert count_new_evidence(store, head, head) == 0
    r = evaluate_delta(store, head, head)
    assert r.count == 0 and r.fired is False


def test_new_evidence_events_returns_the_actual_events(tmp_path):
    store = create_program("p", root=tmp_path)
    base = store.head()
    _append_measurements(store, 4, node_id="H1", start=0)
    evs = new_evidence_events(store, base)
    assert len(evs) == 4 and all(e["kind"] == "measurement" for e in evs)


# ---------------------------------------------------------------------------
# trigger verbs — falsifier / manual / dispatch
# ---------------------------------------------------------------------------

def test_evaluate_falsifier_fires_when_predicate_fires():
    node = make_node(id="H1", falsifiers=[lambda ev: ev.get("value", 1) < 0])
    r_fire = evaluate_falsifier(node, {"value": -1})
    assert r_fire.fired is True and r_fire.verb == "falsifier"
    r_no = evaluate_falsifier(node, {"value": 5})
    assert r_no.fired is False


def test_manual_trigger_always_fires():
    r = manual_trigger()
    assert r.fired is True and r.verb == "manual"
    assert manual_trigger(reason="operator asked").reason == "operator asked"


def test_evaluate_trigger_dispatch(tmp_path):
    store = create_program("p", root=tmp_path)
    base = store.head()
    _append_measurements(store, 10, start=0)
    assert evaluate_trigger("delta", store=store, base_ref=base).fired is True

    node = make_node(id="H1", falsifiers=[lambda ev: True])
    assert evaluate_trigger("falsifier", node=node, evidence={}).fired is True
    assert evaluate_trigger("manual").fired is True
    with pytest.raises(ObserveError):
        evaluate_trigger("nonsense")


# ---------------------------------------------------------------------------
# ACCEPT 3 — recurrence binding: the OFFLINE one-tick test (P5 pattern)
# ---------------------------------------------------------------------------

def test_observer_profile_envelope_shape():
    prof = observer_profile("obs-bobpipe", cron="*/5 * * * *",
                            program_id="bobpipe", source_id="uplift",
                            face_hint="researcher", owner="admin")
    assert prof["name"] == "obs-bobpipe"
    sch = prof["schedule"]
    assert sch["cron"] == "*/5 * * * *"
    assert sch["task"] == observer_task("bobpipe", "uplift")
    assert sch["face_hint"] == "researcher" and sch["owner"] == "admin"


def test_observer_profile_rejects_bad_cron():
    with pytest.raises(ObserveError):
        observer_profile("bad", cron="not a cron", program_id="p", source_id="s")


def test_parse_observer_task_roundtrips():
    task = observer_task("bobpipe", "uplift")
    assert parse_observer_task(task) == {"program": "bobpipe", "source": "uplift"}
    assert parse_observer_task("chat: hi there") is None


async def test_one_tick_fires_observer_profile_offline():
    """The P5 binding, proven OFFLINE: a due observer profile is claimed + invoked in exactly one
    tick, and build_schedule_seed compiles a seed whose task IS the observer one-shot command."""
    prof = observer_profile("obs-bobpipe", cron="*/5 * * * *",
                            program_id="bobpipe", source_id="uplift")
    now = datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC)
    bucket_iso = "2026-06-23T14:30:00+00:00"

    claimed: set = set()

    async def fake_claim(name, bucket, fired_at):
        key = (name, bucket)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    calls: list = []

    async def fake_invoke(profile, schedule, bkt):
        calls.append((profile["name"], schedule["task"], bkt))
        return {"ok": True}

    fired = await run_tick(now, lambda: [prof], fake_claim, fake_invoke, catchup_seconds=120)
    assert fired == [("obs-bobpipe", bucket_iso)]
    assert calls == [("obs-bobpipe", observer_task("bobpipe", "uplift"), bucket_iso)]

    # a second tick in the SAME window must not re-fire (exactly-once via the claim).
    fired2 = await run_tick(now, lambda: [prof], fake_claim, fake_invoke, catchup_seconds=120)
    assert fired2 == []

    # the compiled seed carries the observer one-shot task (routes through the research arm).
    seed = build_schedule_seed(prof, prof["schedule"], bucket_iso)
    assert seed["task"] == observer_task("bobpipe", "uplift")
    assert seed["profile_name"] == "obs-bobpipe"
    assert parse_observer_task(seed["task"]) == {"program": "bobpipe", "source": "uplift"}


# ---------------------------------------------------------------------------
# thin forest sources registry (F0 reuse call)
# ---------------------------------------------------------------------------

def test_forest_source_validation():
    with pytest.raises(Exception):
        ForestSource(id="", kind="testpipe_uplift", node_id="H1", metric="m")
    with pytest.raises(Exception):
        ForestSource(id="x", kind="web-changelog", node_id="H1", metric="m")   # web kind rejected
    with pytest.raises(Exception):
        ForestSource(id="x", kind="testpipe_uplift", node_id="", metric="m")   # node_id required


def test_forest_sources_registry_catalog_and_dedup():
    reg = ForestSourcesRegistry.model_validate({
        "version": 0,
        "sources": [
            {"id": "a", "kind": "testpipe_uplift", "node_id": "H1", "metric": "uplift"},
            {"id": "b", "kind": "flight_spend", "node_id": "H2", "metric": "cost", "enabled": False},
        ],
    })
    assert reg.ids() == ["a", "b"]
    assert [s.id for s in reg.enabled_sources()] == ["a"]
    assert reg.get("a").kind == "testpipe_uplift"
    with pytest.raises(KeyError):
        reg.get("missing")


def test_forest_sources_registry_rejects_bad_version_and_dups():
    with pytest.raises(Exception):
        ForestSourcesRegistry.model_validate({"version": 1, "sources": [
            {"id": "a", "kind": "testpipe_uplift", "node_id": "H1", "metric": "m"}]})
    with pytest.raises(Exception):
        ForestSourcesRegistry.model_validate({"version": 0, "sources": [
            {"id": "a", "kind": "testpipe_uplift", "node_id": "H1", "metric": "m"},
            {"id": "a", "kind": "eval_results", "node_id": "H2", "metric": "m"}]})
    with pytest.raises(Exception):
        ForestSourcesRegistry.model_validate({"version": 0, "sources": []})


def test_forest_sources_registry_schedule_profile_reuses_watchschedule():
    reg = ForestSourcesRegistry.model_validate({
        "version": 0,
        "sources": [{"id": "a", "kind": "testpipe_uplift", "node_id": "H1", "metric": "m"}],
        "schedule": {"cron": "*/5 * * * *", "task": "observe a"},
    })
    prof = reg.schedule_profile("cat")
    assert prof["schedule"]["cron"] == "*/5 * * * *"
    assert prof["schedule"]["task"] == "observe a"
    with pytest.raises(ValueError):
        reg.schedule_profile("bad", cron="not a cron", task="x")


def test_forest_sources_registry_from_yaml(tmp_path):
    p = tmp_path / "forest_sources.yaml"
    p.write_text(
        "version: 0\n"
        "sources:\n"
        "  - id: uplift\n"
        "    kind: testpipe_uplift\n"
        "    node_id: H1\n"
        "    metric: uplift\n",
        encoding="utf-8",
    )
    reg = ForestSourcesRegistry.from_yaml(p)
    assert reg.ids() == ["uplift"] and reg.get("uplift").node_id == "H1"


# ---------------------------------------------------------------------------
# inv.13 — observe.py stays offline/deterministic (no network / clock / random)
# ---------------------------------------------------------------------------

def test_observe_module_is_offline_and_deterministic():
    import pathlib
    src = pathlib.Path(observe.__file__).read_text(encoding="utf-8")
    for banned in ("import socket", "import requests", "urllib", "http.client", "aiohttp",
                   "time.time(", "datetime.now(", "utcnow(", "monotonic(", "random.", "uuid."):
        assert banned not in src, f"observe.py must stay offline/deterministic; found {banned!r}"
