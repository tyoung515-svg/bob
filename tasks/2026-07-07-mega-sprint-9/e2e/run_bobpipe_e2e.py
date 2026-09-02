#!/usr/bin/env python
"""run_bobpipe_e2e.py — the MS9-FE2E RUNNABLE HARNESS (bobpipe seed program, weeks-in-minutes).

Drives the SAME 8-stage research-forest cycle as ``tests/forest/test_forest_e2e.py`` (via the shared
``bobpipe_cycle.run_cycle`` driver), but against bobclaw's OWN Qdrant on :6353 — a UNIQUELY-NAMED
TEST collection created for this run and DROPPED at teardown (mega-sprint invariant 2; the live
``bobclaw__768`` / plain ``research_forest`` collections are never touched). The embedder is bobclaw's
on-demand embedder on :8081 if reachable, else deterministic fixture vectors. Program ledgers live in a
throwaway temp forest root OUTSIDE the repo tree (inv. 12), removed at teardown.

It writes the run's REAL ARTIFACTS under ``e2e/artifacts/``:
  * ``fork_<id>.json``          — the proposal-only fork artifact (written by F7 ``propose_fork``)
  * ``epoch_digest.md``         — the W2-rendered epoch digest (markdown)
  * ``epoch_digest.doc.json``   — the content-addressed W2 digest doc
  * ``ledger_events.json``      — the program ledger's event stream at HEAD (ledger-truth)
  * ``cycle_summary.json``      — the per-stage outcome summary

Honest scope (inv. 13): synthetic backfill + the F4 MOCKED arm — NO new live fleet pass. The one
sanctioned micro live testpipe-uplift run was SPENT by F6 (``sprints/RESULTS-F6.md``); this harness
cites it and does not repeat it.

Run (from repo root):
    .venv/Scripts/python.exe tasks/2026-07-07-mega-sprint-9/e2e/run_bobpipe_e2e.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import shutil
import sys
import tempfile
import urllib.request
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
_CORE = _REPO / "bobclaw-core"
# make `core.*` and `bobpipe_cycle` importable
for p in (str(_CORE), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("MEMORY_ENABLED", "false")

import bobpipe_cycle  # noqa: E402
from core.forest.program import ForestRegistry  # noqa: E402
from core.forest.projection import ForestProjection, deterministic_embedder  # noqa: E402

_QDRANT_URL = os.getenv("FOREST_QDRANT_URL", "http://localhost:6353")
_EMBED_URL = os.getenv("FOREST_EMBED_URL", "http://localhost:8081")
_FIXTURE_DIM = 64
_LIVE_PROTECTED = {"bobclaw__768", "research_forest"}  # never touch these


def _live_embedder_or_none():
    """A sync embed callable hitting :8081/v1/embeddings, or None if unreachable/misbehaving."""
    try:
        req = urllib.request.Request(f"{_EMBED_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return None
    except Exception:
        return None

    def embed(texts):
        out = []
        for t in texts:
            body = json.dumps({"input": t, "model": "embed"}).encode("utf-8")
            req = urllib.request.Request(f"{_EMBED_URL}/v1/embeddings", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out.append([float(x) for x in data["data"][0]["embedding"]])
        return out

    try:
        v = embed(["probe"])
        if not v or not v[0] or not any(abs(x) > 1e-9 for x in v[0]):
            return None
    except Exception:
        return None
    return embed


def _make_client():
    """Return (client, mode). Prefer the real Qdrant on :6353; else fall back to qdrant local mode."""
    try:
        from qdrant_client import QdrantClient
    except Exception as exc:
        print(f"  [projection] qdrant_client not importable ({exc}); using local :memory: fallback")
        return None, "unavailable"
    try:
        client = QdrantClient(url=_QDRANT_URL, timeout=10)
        client.get_collections()  # probe reachability
        return client, "live:6353"
    except Exception as exc:
        print(f"  [projection] Qdrant :6353 unreachable ({exc}); falling back to local :memory:")
        try:
            return QdrantClient(location=":memory:"), "local:memory"
        except Exception as exc2:
            print(f"  [projection] local :memory: also unavailable ({exc2})")
            return None, "unavailable"


def main() -> int:
    # Windows consoles / piped stdout default to cp1252 (strict) — force utf-8 so the ≥/→/— glyphs
    # in the stage details never crash the run.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    artifacts = _HERE / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    forest_root = pathlib.Path(tempfile.mkdtemp(prefix="fe2e_forest_"))
    test_collection = f"research_forest__test_fe2e_{uuid.uuid4().hex[:12]}"
    assert test_collection not in _LIVE_PROTECTED

    embed = _live_embedder_or_none()
    embed_mode = "live:8081" if embed is not None else "fixture(64)"
    embedder = embed if embed is not None else deterministic_embedder(_FIXTURE_DIM)

    client, qmode = _make_client()

    print("=" * 78)
    print("MS9-FE2E — bobpipe seed program, weeks-in-minutes (RUNNABLE HARNESS)")
    print("=" * 78)
    print(f"  standing question : {bobpipe_cycle.STANDING_QUESTION}")
    print(f"  forest root       : {forest_root}  (temp, outside repo — inv.12)")
    print(f"  qdrant            : {qmode}  collection={test_collection}  (dropped at teardown — inv.2)")
    print(f"  embedder          : {embed_mode}")
    print(f"  live carve-out    : SPENT by F6 (sprints/RESULTS-F6.md) — no new live pass (inv.13)")
    print("-" * 78)

    result = None
    exit_code = 1
    try:
        if client is None:
            print("  [projection] no Qdrant client available — cannot run the projection stage.")
            return 2
        registry = ForestRegistry(root=forest_root / "forest")
        proj = ForestProjection(client, embedder=embedder, collection=test_collection)

        def _on_stage(log):
            mark = "OK " if log.ok else "!! "
            print(f"  [{mark}] stage {log.n}: {log.name}\n           {log.detail}")

        result = asyncio.run(bobpipe_cycle.run_cycle(
            registry=registry,
            proj=proj,
            program_id="bobpipe-uplift",
            artifact_dir=artifacts,
            meta_ledger_path=forest_root / "bobpipe__meta.jsonl",
            on_stage=_on_stage,
        ))

        # ── write the real artifacts ────────────────────────────────────────
        store = registry.open_store(result.program_id)
        (artifacts / "epoch_digest.md").write_text(result.digest_markdown, encoding="utf-8")
        (artifacts / "epoch_digest.doc.json").write_text(
            json.dumps(result.digest_doc, indent=2), encoding="utf-8")
        (artifacts / "ledger_events.json").write_text(
            json.dumps(store.read()["events"], indent=2), encoding="utf-8")

        summary = {
            "program_id": result.program_id,
            "standing_question": result.standing_question,
            "qdrant_mode": qmode,
            "test_collection": test_collection,
            "embedder_mode": embed_mode,
            "n_backfilled": result.n_backfilled,
            "delta": {"count": result.delta_count, "threshold": result.delta_threshold,
                      "fired": result.delta_fired},
            "epoch": {"passes": result.epoch_passes, "halted": result.epoch_halted,
                      "blocked": result.epoch_blocked,
                      "verified": result.epoch_verified_ids,
                      "total_est_cost_usd": result.epoch_total_est_cost,
                      "budget_cap_usd": result.budget_cap,
                      "budget_cap_binds": result.budget_cap_binds},
            "ab_race": {"judged": result.ab_judged, "winner": result.ab_winner,
                        "delta": result.ab_delta, "merge_sha": result.ab_merge_sha},
            "entailment_tags": result.tags,
            "fork_proposal": {"child": result.fork_proposal.child_program_id,
                              "fork_id": result.fork_proposal.fork_id,
                              "seed_key": result.fork_proposal.seed_key,
                              "artifact": result.fork_proposal.artifact_filename,
                              "auto_applied": result.fork_auto_applied},
            "projection": {"count": result.projection_count,
                           "fresh_after_rebuild": result.fresh_after_rebuild,
                           "stale_after_advance": result.stale_after_advance,
                           "fresh_after_second_rebuild": result.fresh_after_second_rebuild,
                           "retrieve_hits": result.retrieve_hits},
            "digest": {"digest_id": result.digest_id, "n_alerts": len(result.digest_doc["alerts"])},
            "stages": [dataclasses.asdict(s) for s in result.stages],
            "all_stages_ok": bobpipe_cycle.all_stages_ok(result),
            "live_carve_out": "SPENT by F6 (sprints/RESULTS-F6.md); no new live pass (inv.13)",
        }
        (artifacts / "cycle_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        print("-" * 78)
        ok = bobpipe_cycle.all_stages_ok(result)
        print(f"  ALL STAGES OK: {ok}   artifacts → {artifacts}")
        for name in ("epoch_digest.md", "epoch_digest.doc.json", "ledger_events.json",
                     "cycle_summary.json", result.fork_proposal.artifact_filename):
            print(f"    - {name}")
        exit_code = 0 if ok else 1
    finally:
        # teardown: drop the TEST collection (inv.2) + remove the temp forest root (inv.12)
        try:
            if client is not None and qmode == "live:6353" and client.collection_exists(test_collection):
                client.delete_collection(collection_name=test_collection)
                print(f"  [teardown] dropped TEST collection {test_collection}")
        except Exception as exc:
            print(f"  [teardown] WARN could not drop {test_collection}: {exc}")
        shutil.rmtree(forest_root, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
