"""MS9-F1 — the invariant 11/12 discipline test.

Ledger is TRUTH and the ``core/ledger`` + ``core/memory`` packages are READ-ONLY to the forest:
F1 imports their query fns but must make NO writes under those source paths. This test snapshots
every source file under ``core/ledger/`` and ``core/memory/`` (content hash), runs a FULL F1
exercise (registry create + a fork child + ledger appends, all under a tmp root), and asserts the
snapshot is byte-identical afterwards — and that the program repos land under the tmp data dir, never
in the source tree.
"""
from __future__ import annotations

import hashlib
import pathlib

from core.forest import events as fe
from core.forest import store as forest_store
from core.forest.program import ForestRegistry

_CORE_DIR = pathlib.Path(forest_store.__file__).resolve().parents[1]   # bobclaw-core/core
_LEDGER_DIR = _CORE_DIR / "ledger"
_MEMORY_DIR = _CORE_DIR / "memory"


def _snapshot(root: pathlib.Path) -> dict[str, str]:
    """Map each non-bytecode source file (relative path) to a sha256 of its bytes."""
    snap: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def test_f1_makes_no_writes_under_core_ledger_or_memory(tmp_path):
    before_ledger = _snapshot(_LEDGER_DIR)
    before_memory = _snapshot(_MEMORY_DIR) if _MEMORY_DIR.exists() else {}
    assert before_ledger, "expected core/ledger to contain source files"

    # A full F1 exercise, entirely under the tmp root (the gitignored-equivalent data dir).
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("parent-program", question="seed")
    reg.create_program("child-program", parent="parent-program")   # fork provenance edge
    store = reg.open_store("parent-program")
    store.append_events([
        fe.measurement(node_id="H1", metric="uplift", value=0.42, source="testpipe", ts=1000),
        fe.spend(amount_usd=0.03, label="observer", ts=1000),
        fe.fork_proposed(fork_id="F1", parent_program="parent-program", rationale="outgrew tree", ts=1001),
    ])

    after_ledger = _snapshot(_LEDGER_DIR)
    after_memory = _snapshot(_MEMORY_DIR) if _MEMORY_DIR.exists() else {}

    assert after_ledger == before_ledger, "F1 must not write/modify any file under core/ledger/"
    assert after_memory == before_memory, "F1 must not write/modify any file under core/memory/"


def test_program_repos_land_under_the_data_root_not_the_source_tree(tmp_path):
    reg = ForestRegistry(root=tmp_path)
    reg.create_program("prog-a")
    store = reg.open_store("prog-a")
    resolved = store.repo.resolve()
    assert tmp_path.resolve() in resolved.parents
    # explicitly NOT inside the core source packages
    assert _LEDGER_DIR not in resolved.parents
    assert _CORE_DIR not in resolved.parents


def test_default_forest_root_is_inside_the_gitignored_data_dir():
    # The default program-repo home is bobclaw-core/data/forest — and data/* is gitignored,
    # so no program repo is ever committable into the tree (inv. 12).
    default_root = forest_store.DEFAULT_FOREST_ROOT.resolve()
    assert default_root.name == "forest"
    assert default_root.parent.name == "data"
    assert default_root.parent.parent == _CORE_DIR.parent   # bobclaw-core/data
