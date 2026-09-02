"""core.forest — Research Forest substrate (MS9-F1).

Per-program git-backed ledgers (TRUTH) + a program registry with fork provenance edges.
Ledger is truth; the LKS projection (F8) is a derived view. This package writes ONLY program
ledger repos under the gitignored data dir (``bobclaw-core/data/forest/<program_id>/``); it imports
the ``core/ledger`` query primitives READ-ONLY and never writes any file under ``core/ledger/`` or
``core/memory/`` (mega-sprint invariants 11/12).

Public surface:
  * events.py — typed constructors/validators for the 9 additive forest event kinds.
  * store.py  — create/open a per-program git-backed ledger repo + append events.
  * program.py — the forest registry (program index + provenance DAG).
"""
from __future__ import annotations

from core.forest.events import (  # noqa: F401
    EVENT_KINDS,
    REQUIRED_FIELDS,
    ForestError,
    ForestEventError,
    archive_proposed,
    epoch_close,
    epoch_open,
    experiment_run,
    fork_approved,
    fork_proposed,
    is_forest_event,
    measurement,
    pull_from_parent,
    spend,
    validate_event,
)
from core.forest.experiment import (  # noqa: F401
    ExperimentError,
    ExperimentGate,
    ExperimentNode,
    assess_experiment,
    experiment_approval_item,
    gate_experiment,
)
from core.forest.program import (  # noqa: F401
    ForestRegistry,
    ProgramRecord,
    ProgramRegistryError,
)
from core.forest.store import (  # noqa: F401
    DEFAULT_FOREST_ROOT,
    ProgramStore,
    ProgramStoreError,
    create_program,
    exists,
    open_program,
    program_path,
    validate_program_id,
)

__all__ = [
    # errors
    "ForestError",
    "ForestEventError",
    "ProgramStoreError",
    "ProgramRegistryError",
    # events
    "EVENT_KINDS",
    "REQUIRED_FIELDS",
    "validate_event",
    "is_forest_event",
    "measurement",
    "spend",
    "epoch_open",
    "epoch_close",
    "fork_proposed",
    "fork_approved",
    "pull_from_parent",
    "experiment_run",
    "archive_proposed",
    # store
    "DEFAULT_FOREST_ROOT",
    "ProgramStore",
    "validate_program_id",
    "program_path",
    "exists",
    "create_program",
    "open_program",
    # registry
    "ProgramRecord",
    "ForestRegistry",
    # experiment (F6)
    "ExperimentError",
    "ExperimentNode",
    "ExperimentGate",
    "assess_experiment",
    "gate_experiment",
    "experiment_approval_item",
]
