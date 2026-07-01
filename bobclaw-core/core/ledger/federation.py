from __future__ import annotations

import copy
import json
import os
import pathlib
import dataclasses
from typing import Any, Dict, List

from core.ledger import project
from core.ledger.gitdag import _git, GitError

REGISTRY_VERSION = 1
DEFAULT_LEDGER_DIR = "ledger"


class FederationError(RuntimeError):
    """Registry-level error: unknown instance, duplicate name/collection, bad record."""
    pass


def default_registry_path() -> pathlib.Path:
    """Return the default path to the federation registry JSON file.

    If the environment variable BOBCLAW_LEDGER_INSTANCES is set, use it verbatim.
    Otherwise, return `<bobclaw-core>/data/ledger_instances.json` computed from
    this module's location.
    """
    env_path = os.environ.get("BOBCLAW_LEDGER_INSTANCES")
    if env_path:
        return pathlib.Path(env_path)
    # __file__ resolves to core/ledger/federation.py; parents[2] gives the project root.
    return pathlib.Path(__file__).resolve().parents[2] / "data" / "ledger_instances.json"


@dataclasses.dataclass(frozen=True)
class ResolvedInstance:
    """A fully resolved ledger instance with bound projection entrypoints."""
    name: str
    repo: str
    ledger_dir: str
    collection: str
    dim: int
    meta: dict

    def read_ledger_at(self, ref: str = "HEAD") -> dict:
        """Delegate to `project.read_ledger_at` using this instance's repo and ledger_dir."""
        return project.read_ledger_at(self.repo, ref, ledger_dir=self.ledger_dir)

    def projection_key(self, ref: str = "HEAD") -> str:
        """Delegate to `project.projection_key` using this instance's repo and ledger_dir."""
        return project.projection_key(self.repo, ref, ledger_dir=self.ledger_dir)

    def diff_ledger(self, base: str, head: str) -> dict:
        """Delegate to `project.diff_ledger` using this instance's repo and ledger_dir."""
        return project.diff_ledger(self.repo, base, head, ledger_dir=self.ledger_dir)

    def is_git(self) -> bool:
        """Return True iff the instance's repo is inside a git work tree.

        Uses the single git call site (`_git`). Never raises; a missing or non-git
        directory returns False.
        """
        try:
            result = _git(self.repo, "rev-parse", "--is-inside-work-tree", allow_fail=True)
            return result.returncode == 0 and result.stdout.strip() == "true"
        except (FileNotFoundError, GitError, OSError):
            return False


def _validate_record(record: dict, *, name: str) -> None:
    """Validate a raw instance record dict.

    Raises `FederationError` if any required field is missing, empty, or of wrong type.
    """
    # required string fields
    for field in ("repo", "collection"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise FederationError(
                f"Instance '{name}' requires a non-empty '{field}' string"
            )
    # ledger_dir must be non-empty string
    ledger_dir = record.get("ledger_dir")
    if not isinstance(ledger_dir, str) or not ledger_dir.strip():
        raise FederationError(
            f"Instance '{name}' requires a non-empty 'ledger_dir' string"
        )
    # dim must be a positive int, not bool, not float
    dim = record.get("dim")
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise FederationError(
            f"Instance '{name}' requires 'dim' to be a positive integer (got {dim!r})"
        )
    # meta must be dict if present
    meta = record.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise FederationError(
            f"Instance '{name}' 'meta' must be a dict or absent (got {meta!r})"
        )


class FederationRegistry:
    """In-memory federation registry backed by a JSON file.

    Holds a dict of instance records keyed by name. All CRUD operations work
    in memory; call `save()` to persist.
    """

    def __init__(self, path: str | pathlib.Path | None = None):
        """Initialize with a registry file path; does not touch disk."""
        self.path = pathlib.Path(path) if path else default_registry_path()
        self._instances: Dict[str, dict] = {}

    # ---- persistence ----

    def load(self) -> "FederationRegistry":
        """Read the JSON file and populate the in-memory registry.

        A missing file results in an empty registry. A malformed file raises
        FederationError.
        """
        if not self.path.exists():
            self._instances = {}
            return self

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise FederationError(
                f"Failed to parse registry file '{self.path}': {exc}"
            ) from exc

        if not isinstance(data, dict) or "instances" not in data:
            raise FederationError(
                f"Registry file '{self.path}' must contain a top-level dict with 'instances' key"
            )

        instances = data["instances"]
        if not isinstance(instances, dict):
            raise FederationError(
                f"Registry file '{self.path}': 'instances' must be a dict"
            )

        loaded: Dict[str, dict] = {}
        for name, record in instances.items():
            if not isinstance(name, str) or not name.strip():
                raise FederationError(
                    f"Registry file '{self.path}': instance name must be a non-empty string"
                )
            if not isinstance(record, dict):
                raise FederationError(
                    f"Registry file '{self.path}': instance '{name}' record must be a dict"
                )
            _validate_record(record, name=name)
            loaded[name] = record
        # Cross-record invariant: collection -> repo must be a TOTAL map, so enforce
        # collection-uniqueness on load too (register()/update() already do on the write path).
        # A hand-edited file with two instances sharing a collection is rejected, not silently
        # accepted (which would make by_collection() iteration-order-dependent).
        seen: Dict[str, str] = {}
        for name, record in loaded.items():
            coll = record["collection"]
            if coll in seen:
                raise FederationError(
                    f"Registry file '{self.path}': collection '{coll}' is assigned to both "
                    f"'{seen[coll]}' and '{name}'"
                )
            seen[coll] = name
        self._instances = loaded
        return self

    def save(self) -> "FederationRegistry":
        """Atomically write the current registry to the JSON file.

        Creates parent directories if missing. Uses a temporary sibling file
        and `os.replace` for atomicity.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp" + self.path.suffix)
        data = {
            "version": REGISTRY_VERSION,
            "instances": self._instances,
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception:
            # Atomicity contract: a failed serialize (e.g. a non-JSON-serializable meta) must NOT
            # leave an orphan tmp file behind, and — because os.replace() is the LAST step — never
            # corrupts/partially-writes the real registry file (it stays as it was before save()).
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
        return self

    # ---- CRUD ----

    def register(
        self,
        name: str,
        repo: str,
        *,
        collection: str,
        dim: int,
        ledger_dir: str = DEFAULT_LEDGER_DIR,
        meta: dict | None = None,
        overwrite: bool = False,
    ) -> dict:
        """Register a new instance record.

        Validates all fields, then stores in memory.  Does **not** auto-save.
        Raises `FederationError` if name already exists (unless `overwrite=True`)
        or if collection is already mapped to a different instance.
        """
        if not isinstance(name, str) or not name.strip():
            raise FederationError("Instance name must be a non-empty string")

        record: dict = {
            "repo": repo,
            "collection": collection,
            "dim": dim,
            "ledger_dir": ledger_dir,
            "meta": meta if meta is not None else {},
        }
        _validate_record(record, name=name)

        # reject duplicate name unless overwrite
        if name in self._instances and not overwrite:
            raise FederationError(
                f"Instance '{name}' already registered (use overwrite=True to replace)"
            )

        # collection must be unique across registry
        self._check_collection_unique(collection, exclude=name)

        # Store a deep copy so a caller mutating the `meta` they passed in cannot reach into
        # internal state; return via get() (also a deep copy).
        self._instances[name] = copy.deepcopy(record)
        return self.get(name)

    def update(self, name: str, **fields: Any) -> dict:
        """Partially update an existing instance.

        Accepts any of `repo`, `collection`, `dim`, `ledger_dir`, `meta`.
        Validates the merged record and collection uniqueness.  Returns the
        updated record (including "name").
        """
        if name not in self._instances:
            raise FederationError(f"Unknown instance '{name}'")

        record = dict(self._instances[name])
        valid_keys = {"repo", "collection", "dim", "ledger_dir", "meta"}
        for key, value in fields.items():
            if key not in valid_keys:
                raise FederationError(f"Invalid field '{key}' for update")
            record[key] = value
        # ensure ledger_dir and meta defaults; normalize meta=None -> {} so update() matches
        # register() (which already does `meta if meta is not None else {}`).
        record.setdefault("ledger_dir", DEFAULT_LEDGER_DIR)
        if record.get("meta") is None:
            record["meta"] = {}

        _validate_record(record, name=name)

        # if collection changed, check uniqueness (excluding self)
        new_collection = record["collection"]
        old_collection = self._instances[name].get("collection")
        if new_collection != old_collection:
            self._check_collection_unique(new_collection, exclude=name)

        self._instances[name] = copy.deepcopy(record)
        return self.get(name)

    def unregister(self, name: str) -> None:
        """Remove the instance with the given name.  Raises `FederationError` if unknown."""
        if name not in self._instances:
            raise FederationError(f"Unknown instance '{name}'")
        del self._instances[name]

    def get(self, name: str) -> dict:
        """Return the record (including 'name') for the instance.

        A DEEP COPY — a caller mutating the returned dict (or its nested ``meta``)
        cannot corrupt internal registry state.
        """
        if name not in self._instances:
            raise FederationError(f"Unknown instance '{name}'")
        out = copy.deepcopy(self._instances[name])
        out["name"] = name
        return out

    def list(self) -> list[dict]:
        """Return all records (each including 'name'), sorted by name."""
        return [self.get(name) for name in sorted(self._instances)]

    def names(self) -> list[str]:
        """Return sorted instance names."""
        return sorted(self._instances.keys())

    def by_collection(self, collection: str) -> dict:
        """Reverse lookup: return the record whose collection equals the given string.

        Raises `FederationError` if not found (uniqueness is enforced on write,
        so at most one match).
        """
        for name, record in self._instances.items():
            if record["collection"] == collection:
                out = copy.deepcopy(record)
                out["name"] = name
                return out
        raise FederationError(f"Collection '{collection}' not found in registry")

    # ---- resolution ----

    def resolve(self, name: str) -> ResolvedInstance:
        """Build a fully resolved instance from the stored record.

        Does **not** require the repo to exist or be git.
        """
        record = self.get(name)  # raises FederationError if unknown (returns a deep copy)
        return ResolvedInstance(
            name=record["name"],
            repo=record["repo"],
            ledger_dir=record["ledger_dir"],
            collection=record["collection"],
            dim=record["dim"],
            # `or {}` (not `get(..., {})`) so a stored/loaded record with meta == None
            # (e.g. JSON `"meta": null`) still yields a dict, honoring the dataclass contract.
            meta=record.get("meta") or {},
        )

    # ---- internal helpers ----

    def _check_collection_unique(self, collection: str, *, exclude: str | None = None) -> None:
        """Raise `FederationError` if the collection is already used by a different instance.

        :param exclude: if set, skip the instance with this name.
        """
        for existing_name, existing_record in self._instances.items():
            if existing_name == exclude:
                continue
            if existing_record["collection"] == collection:
                raise FederationError(
                    f"Collection '{collection}' is already assigned to instance "
                    f"'{existing_name}'"
                )
