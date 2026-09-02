"""program.py — the forest REGISTRY.

A program index (id -> metadata) + provenance edges (parent/child), the seam future forks (F7) build
on. It is a CATALOG (a JSON file, ``registry.json`` in the forest root), NOT a measurement store:
measurement/spend/epoch/etc. TRUTH lives in each program's git ledger (``store.py`` / ``events.py``).
This registry only tracks which programs exist, their metadata, and the fork-provenance DAG. It
reuses ``store.py`` for the actual git ledger repo (it never re-implements git plumbing, and it never
writes measurement events).
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass, field

from core.forest.events import ForestError
from core.forest.store import (
    DEFAULT_FOREST_ROOT,
    ProgramStore,
    create_program as _store_create,
    exists as _store_exists,
    open_program as _store_open,
    validate_program_id,
)

__all__ = ["ProgramRegistryError", "ProgramRecord", "ForestRegistry"]


class ProgramRegistryError(ForestError):
    """Raised on registry-level errors (duplicate, missing, invalid parent)."""
    pass


@dataclass
class ProgramRecord:
    """Metadata record for a single program in the forest."""

    id: str
    question: str | None = None
    metadata: dict = field(default_factory=dict)
    parent: str | None = None
    children: list = field(default_factory=list)
    created_at_ref: str | None = None  # the initial ledger commit sha

    def to_dict(self) -> dict:
        """Convert this record to a JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProgramRecord":
        """Rebuild a record from its serialized dict."""
        return cls(
            id=d["id"],
            question=d.get("question"),
            metadata=dict(d.get("metadata") or {}),
            parent=d.get("parent"),
            children=list(d.get("children") or []),
            created_at_ref=d.get("created_at_ref"),
        )


class ForestRegistry:
    """Persists the program catalog (id -> record) and the fork-provenance DAG.

    Persistence is an ATOMIC write (temp + ``os.replace``) to ``registry.json`` inside the forest
    root. All methods operate on a lazily-loaded in-memory cache.
    """

    def __init__(self, root=None):
        self.root = pathlib.Path(root) if root is not None else DEFAULT_FOREST_ROOT
        self._index_path = self.root / "registry.json"
        self._programs: dict[str, ProgramRecord] | None = None

    def _load(self) -> dict[str, ProgramRecord]:
        """Lazy-load the registry from disk and return the id -> record dict."""
        if self._programs is not None:
            return self._programs
        if self._index_path.exists():
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._programs = {
                pid: ProgramRecord.from_dict(rec)
                for pid, rec in data.get("programs", {}).items()
            }
        else:
            self._programs = {}
        return self._programs

    def _save(self) -> None:
        """Atomically write the registry to disk (temp file + ``os.replace``)."""
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"programs": {pid: rec.to_dict() for pid, rec in self._programs.items()}}
        tmp = self._index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self._index_path)

    def create_program(
        self,
        program_id: str,
        *,
        question: str | None = None,
        metadata: dict | None = None,
        parent: str | None = None,
        exist_ok: bool = False,
    ) -> ProgramRecord:
        """Register a new program (and create its git ledger repo).

        With ``parent`` set, records a BIDIRECTIONAL provenance edge (``child.parent`` +
        ``parent.children``). Raises ``ProgramRegistryError`` on a duplicate (unless *exist_ok*) or an
        unregistered parent.

        Crash-consistency: the git ledger repo and ``registry.json`` are two resources, so a crash
        between creating the repo and saving the index can leave an ORPHAN ledger on disk with no
        registry record. Rather than let that wedge the id (``_store_create`` would later raise a
        confusing "already exists"), an un-registered on-disk ledger is ADOPTED on *exist_ok* (its
        ROOT commit becomes ``created_at_ref``); without *exist_ok* a clear, recoverable error is
        raised.
        """
        validate_program_id(program_id)
        progs = self._load()
        if program_id in progs:
            if not exist_ok:
                raise ProgramRegistryError(f"program {program_id!r} already registered")
            # Idempotent re-registration must still honor provenance invariants — the early
            # return here previously bypassed the parent guard + bidirectional-edge block below
            # (audit F1-r5), letting a ghost parent slip through and letting the two sides of the
            # DAG permanently disagree. Re-check them here.
            rec = progs[program_id]
            if parent is not None:
                if parent not in progs:
                    raise ProgramRegistryError(f"parent {parent!r} not registered")
                if rec.parent != parent:
                    raise ProgramRegistryError(
                        f"program {program_id!r} already registered with parent {rec.parent!r}; "
                        f"refusing to re-parent to {parent!r}"
                    )
                # repair a possibly half-written edge (crash between the two children/parent writes)
                prec = progs[parent]
                if program_id not in prec.children:
                    prec.children.append(program_id)
                    prec.children.sort()
                    self._save()
            return rec
        if parent is not None and parent not in progs:
            raise ProgramRegistryError(f"parent {parent!r} not registered")

        if _store_exists(program_id, root=self.root):
            # An on-disk ledger with no registry record: a crash-orphan or an externally-created repo.
            if not exist_ok:
                raise ProgramRegistryError(
                    f"program {program_id!r} has an un-registered ledger on disk; "
                    "pass exist_ok=True to adopt it"
                )
            store = _store_open(program_id, root=self.root)
            created_at_ref = store.root_ref()   # honest creation ref even after prior appends
        else:
            store = _store_create(program_id, root=self.root)
            created_at_ref = store.head()       # fresh repo: head == root commit

        rec = ProgramRecord(
            id=program_id,
            question=question,
            metadata=dict(metadata or {}),
            parent=parent,
            children=[],
            created_at_ref=created_at_ref,
        )
        progs[program_id] = rec

        if parent is not None:
            prec = progs[parent]
            if program_id not in prec.children:
                prec.children.append(program_id)
                prec.children.sort()

        self._save()
        return rec

    def get(self, program_id: str) -> ProgramRecord:
        """Return the record for *program_id*; raise ``ProgramRegistryError`` if unregistered."""
        progs = self._load()
        if program_id not in progs:
            raise ProgramRegistryError(f"program {program_id!r} not registered")
        return progs[program_id]

    def list(self) -> list[ProgramRecord]:
        """Return all records sorted by id (deterministic)."""
        progs = self._load()
        return [progs[k] for k in sorted(progs.keys())]

    def exists(self, program_id: str) -> bool:
        """Return True iff *program_id* is registered."""
        return program_id in self._load()

    def children(self, program_id: str) -> list[str]:
        """Return the sorted child ids of *program_id* (raises if unregistered)."""
        return sorted(list(self.get(program_id).children))

    def open_store(self, program_id: str) -> ProgramStore:
        """Return the :class:`ProgramStore` (git ledger) for a registered program."""
        self.get(program_id)  # raises if absent
        return _store_open(program_id, root=self.root)

    def reload(self) -> None:
        """Drop the in-memory cache so the next access re-reads ``registry.json`` from disk."""
        self._programs = None
