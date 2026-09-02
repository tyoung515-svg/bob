"""store.py — Per-program GIT-BACKED ledger store for forest events.

Creates/opens a git repository in a gitignored data dir (default
``bobclaw-core/data/forest/<program_id>/``) and appends FOREST events in the ``core/ledger``
``events.jsonl`` format. The ledger is TRUTH (mega-sprint invariant 12); this module writes ONLY the
program ledger repo — never the LKS index, and NEVER any file under ``core/ledger/`` or
``core/memory/`` (those are READ-ONLY imports). It reuses the landed ``core/ledger`` git plumbing
(``_git`` / ``commit_trajectory`` / ``head_sha``) and query fns (``read_ledger_at`` /
``projection_key`` / ``diff_ledger``) rather than reinventing git.
"""

import json
import pathlib
import re

from core.forest.events import ForestError, ForestEventError, validate_event
from core.ledger.gitdag import GitError, _git, commit_trajectory, head_sha
from core.ledger.project import diff_ledger, projection_key, read_ledger_at

# ``parents[2]`` == the bobclaw-core dir; ``data/*`` is already gitignored, so this default dir is
# gitignored for free. Tests pass ``root=tmp_path`` so they never touch the real data dir.
DEFAULT_FOREST_ROOT = pathlib.Path(__file__).resolve().parents[2] / "data" / "forest"
_LEDGER_DIR = "ledger"


class ProgramStoreError(ForestError):
    """Error raised for program-store operations (bad id, missing/duplicate program, commit fail)."""
    pass


def validate_program_id(program_id: str) -> str:
    """Validate *program_id* (non-empty str, safe as a single dir name) and return it unchanged.

    Rejects anything that could traverse out of the data dir: it must match
    ``^[A-Za-z0-9][A-Za-z0-9._-]*$`` and contain no ``..`` nor any path separator.
    """
    if not isinstance(program_id, str) or not program_id:
        raise ProgramStoreError("program_id must be a non-empty string")
    if ".." in program_id or "/" in program_id or "\\" in program_id:
        raise ProgramStoreError(
            f"program_id {program_id!r} must not contain '..' or a path separator"
        )
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", program_id):
        raise ProgramStoreError(f"program_id {program_id!r} contains invalid characters")
    return program_id


class ProgramStore:
    """A thin handle on one program's git-backed ledger repo."""

    def __init__(self, program_id: str, repo: pathlib.Path) -> None:
        self.program_id = program_id
        self.repo = pathlib.Path(repo)
        self.ledger_dir = _LEDGER_DIR

    def append_events(self, events, *, message: str | None = None) -> str:
        """Validate, append, and commit *events*; return the new HEAD sha.

        Every event is validated BEFORE anything is written, so a bad batch writes nothing. Each
        event is appended as one compact JSON line to ``ledger/events.jsonl`` and committed via the
        ledger's own ``commit_trajectory`` (ARTIFACT_COMPLETE boundary).
        """
        if not events:
            raise ProgramStoreError("no events to append")
        for ev in events:
            try:
                validate_event(ev)
            except ForestEventError as exc:
                raise ProgramStoreError(f"invalid event: {exc}") from exc

        ledger_dir_path = self.repo / self.ledger_dir
        ledger_dir_path.mkdir(parents=True, exist_ok=True)
        events_path = ledger_dir_path / "events.jsonl"
        with open(events_path, "a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")

        msg = message or f"forest: append {len(events)} event(s)"
        sha = commit_trajectory(
            str(self.repo), msg, paths=[self.ledger_dir], boundary_kind="ARTIFACT_COMPLETE"
        )
        if sha is None:
            raise ProgramStoreError("append produced no commit")
        return sha

    def read(self, ref: str = "HEAD") -> dict:
        """Return the full ledger dict (ref/events/claims/falsifiers) at *ref*."""
        return read_ledger_at(str(self.repo), ref, ledger_dir=self.ledger_dir)

    def events(self, ref: str = "HEAD") -> list:
        """Return the list of events at *ref*."""
        return self.read(ref)["events"]

    def projection_key(self, ref: str = "HEAD") -> str:
        """Return the commit-independent projection key of the ledger tree at *ref*."""
        return projection_key(str(self.repo), ref, ledger_dir=self.ledger_dir)

    def diff(self, base, head: str = "HEAD") -> dict:
        """Return the ledger change-set between *base* and *head*."""
        return diff_ledger(str(self.repo), base, head, ledger_dir=self.ledger_dir)

    def head(self) -> str:
        """Return the full HEAD sha of the program repo."""
        return head_sha(str(self.repo))

    def root_ref(self) -> str:
        """Return the INITIAL (root) commit sha of the program ledger.

        For a freshly-created program this equals :meth:`head`, but it stays correct after appends
        (and when adopting an existing ledger), so it is the honest "program creation" ref.
        """
        result = _git(str(self.repo), "rev-list", "--max-parents=0", "HEAD")
        shas = [s for s in result.stdout.strip().splitlines() if s]
        return shas[0] if shas else self.head()


def program_path(program_id: str, *, root: pathlib.Path | None = None) -> pathlib.Path:
    """Return the on-disk path for a program's repo (after id validation)."""
    validate_program_id(program_id)
    base = pathlib.Path(root) if root is not None else DEFAULT_FOREST_ROOT
    return base / program_id


def exists(program_id: str, *, root: pathlib.Path | None = None) -> bool:
    """Return True iff the program repo directory exists and is a git repo."""
    p = program_path(program_id, root=root)
    return p.is_dir() and (p / ".git").exists()


def create_program(
    program_id: str, *, root: pathlib.Path | None = None, exist_ok: bool = False
) -> ProgramStore:
    """Create a new git-backed program ledger repo and return its :class:`ProgramStore`.

    Raises ``ProgramStoreError`` if the program already exists and *exist_ok* is False. The created
    repo carries a LOCAL git identity + ``commit.gpgsign=false`` so an unattended run never depends
    on global git config nor blocks on a signing prompt.
    """
    validate_program_id(program_id)
    p = program_path(program_id, root=root)
    if exists(program_id, root=root):
        if exist_ok:
            return open_program(program_id, root=root)
        raise ProgramStoreError(f"program {program_id!r} already exists")

    p.mkdir(parents=True, exist_ok=True)
    try:
        _git(p, "init", "-q")
        _git(p, "config", "user.email", "forest@bobclaw.local")
        _git(p, "config", "user.name", "BoBClaw Forest")
        _git(p, "config", "commit.gpgsign", "false")
    except GitError as exc:
        raise ProgramStoreError(f"git init/config failed: {exc}") from exc

    (p / _LEDGER_DIR).mkdir(parents=True, exist_ok=True)
    (p / _LEDGER_DIR / "events.jsonl").write_text("", encoding="utf-8")

    try:
        sha = commit_trajectory(
            str(p),
            f"forest: init program {program_id}",
            paths=[_LEDGER_DIR],
            boundary_kind="ARTIFACT_COMPLETE",
        )
    except GitError as exc:
        raise ProgramStoreError(f"initial commit failed: {exc}") from exc
    # Fail loud (symmetry with append_events): the initial commit MUST produce a resolvable HEAD, so
    # read/head/diff/projection_key work. A None here means the empty ledger was not staged (e.g. a
    # stray ignore rule) — never return a store whose repo has no HEAD.
    if sha is None:
        raise ProgramStoreError(
            f"initial commit for program {program_id!r} produced no commit (empty ledger not staged?)"
        )
    return ProgramStore(program_id, p)


def open_program(program_id: str, *, root: pathlib.Path | None = None) -> ProgramStore:
    """Open an existing program ledger repo and return its :class:`ProgramStore`.

    Raises ``ProgramStoreError`` if the program does not exist.
    """
    validate_program_id(program_id)
    if not exists(program_id, root=root):
        raise ProgramStoreError(f"program {program_id!r} does not exist")
    return ProgramStore(program_id, program_path(program_id, root=root))


__all__ = [
    "DEFAULT_FOREST_ROOT",
    "ProgramStore",
    "ProgramStoreError",
    "validate_program_id",
    "program_path",
    "exists",
    "create_program",
    "open_program",
]
