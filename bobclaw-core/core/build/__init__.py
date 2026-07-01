"""BoBClaw build pipeline — the agentic plan→build→test→repair loop's pure core.

Productionizes ``tasks/2026-06-22-centerpiece-demo/demo_variant_b.py`` into graph
nodes. This package holds the NETWORK-FREE, deterministic pieces:

* :mod:`core.build.contracts` — parse + validate the apex's contract JSON
  (tolerant of truncation), and the ast-based impl extractor.
* :mod:`core.build.skeleton` — deterministic codegen (stub module + pytest suite +
  runnable CLI) from contracts, plus the build-empty gate (imports clean + tests
  collect) run BEFORE any worker fills a contract.

The graph node (:mod:`core.nodes.build_plan`) is the only part that touches the
network (the apex skeleton call) or the filesystem subprocess; everything here is
pure/deterministic and unit-testable without a backend.
"""
