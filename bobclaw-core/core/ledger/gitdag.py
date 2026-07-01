from __future__ import annotations

import pathlib
import re
import subprocess
import unicodedata
from typing import List, Optional

from core.ledger.commits import should_commit


class GitError(RuntimeError):
    """Raised when a git command fails (unless allow_fail=True)."""
    pass


def _git(
    repo: pathlib.Path | str,
    *args: str,
    allow_fail: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a git command in the given repository.

    Args:
        repo: Path to the git repository.
        *args: Git subcommand and arguments (e.g., "status", "--porcelain").
        allow_fail: If True, return the CompletedProcess even on non-zero exit.
            Otherwise raise GitError.

    Returns:
        subprocess.CompletedProcess with stdout/stderr captured (text, utf-8).

    Raises:
        GitError if allow_fail is False and the git command exits with non-zero.
    """
    cmd = ["git", "-C", str(repo), *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0 and not allow_fail:
        raise GitError(f"git {args} failed: {result.stderr.strip()}")
    return result


def is_clean(repo: pathlib.Path | str) -> bool:
    """Check if the repository has no unstaged or staged changes.

    Returns True if `git status --porcelain` is empty.
    """
    result = _git(repo, "status", "--porcelain")
    return result.stdout.strip() == ""


def current_branch(repo: pathlib.Path | str) -> str:
    """Return the current branch name (abbreviated ref).

    Equivalent to `git rev-parse --abbrev-ref HEAD`.
    """
    result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def head_sha(repo: pathlib.Path | str) -> str:
    """Return the full SHA of the HEAD commit."""
    result = _git(repo, "rev-parse", "HEAD")
    return result.stdout.strip()


def normalize_slug(slug: str) -> str:
    """Normalize a string to a URL-friendly slug.

    Steps: lowercase, NFKC (approximated by ASCII), replace non-[a-z0-9] with '-',
    collapse repeated hyphens, strip leading/trailing hyphens.
    """
    # NFKD-decompose so an accented letter folds to its base (é -> e) before ASCII-stripping
    # the combining marks — preserves the letter instead of dropping the whole character.
    s = unicodedata.normalize("NFKD", slug).encode("ascii", "ignore").decode("ascii").lower()
    # Replace non-alphanumeric (except hyphen) with hyphen
    s = re.sub(r"[^a-z0-9]", "-", s)
    # Collapse multiple hyphens into one
    s = re.sub(r"-+", "-", s)
    # Strip leading/trailing hyphens
    s = s.strip("-")
    return s


def branch_run(
    repo: pathlib.Path | str,
    slug: str,
    *,
    date: str,
    base: str = "main",
) -> str:
    """Create and check out a research branch.

    Branch name: research/{date}-{normalize_slug(slug)}.
    Created from `base` (default "main").

    Returns the branch name.

    Raises GitError if the base branch does not exist or other git error.
    """
    # Validate `date` (an injected arg) so it can't smuggle ref-unsafe chars (`..`, `/`) into
    # the branch ref — slug is normalized, date must be too.
    if not re.fullmatch(r"[0-9][0-9-]*", str(date)):
        raise GitError(f"unsafe date for a git ref: {date!r} (expected digits/dashes)")
    # Empty/punctuation-only slug normalizes to "" — fall back to "run" so the ref isn't
    # `research/<date>-` (trailing dash). normalize_slug itself stays a pure normalizer.
    slug_norm = normalize_slug(slug) or "run"
    branch_name = f"research/{date}-{slug_norm}"
    _git(repo, "checkout", "-b", branch_name, base)
    return branch_name


def commit_trajectory(
    repo: pathlib.Path | str,
    message: str,
    *,
    paths: Optional[List[str]] = None,
    boundary_kind: str = "ARTIFACT_COMPLETE",
) -> str | None:
    """Create one commit for a trajectory if there are staged changes.

    Args:
        repo: Repository path.
        message: Commit message (passed as a single argument to -m).
        paths: List of paths to stage (default ["ledger"]).
        boundary_kind: Type of boundary; must pass should_commit().
                       Raises GitError if not committable.

    Returns:
        HEAD SHA of the new commit, or None if nothing was staged.

    Raises:
        GitError if boundary_kind is not committable (e.g. "TOOL_CALL"),
                or if the git commit fails for other reasons.
    """
    if not should_commit(boundary_kind):
        raise GitError(
            f"boundary_kind='{boundary_kind}' is not committable"
        )

    if paths is None:
        paths = ["ledger"]

    # Stage the specified paths
    _git(repo, "add", "--", *paths)

    # Check if anything is staged
    diff_result = _git(repo, "diff", "--cached", "--quiet", allow_fail=True)
    if diff_result.returncode == 0:
        # Nothing staged: no empty commit
        return None

    # Commit with the provided message (passed as a single argument, never shell string)
    _git(repo, "commit", "-m", message)
    return head_sha(repo)


def merge_synthesis(
    repo: pathlib.Path | str,
    branch: str,
    *,
    into: str = "main",
) -> dict:
    """Perform a non-fast-forward merge of `branch` into `into`.

    If conflict occurs, abort the merge and return the list of conflicting files.

    Returns:
        dict with keys:
            - "merged": bool
            - "conflicts": list of paths (sorted) if conflict, else []
            - "commit": HEAD SHA if merged, else None
    """
    # Checkout the target branch
    _git(repo, "checkout", into)

    # Attempt merge with no-fast-forward and no-edit (use default message)
    result = _git(repo, "merge", "--no-ff", "--no-edit", branch, allow_fail=True)

    if result.returncode == 0:
        # Successful synthesis merge
        return {
            "merged": True,
            "conflicts": [],
            "commit": head_sha(repo),
        }

    # Non-zero exit. Distinguish a real merge CONFLICT — UNMERGED paths exist, a merge is in
    # progress and must be aborted — from any OTHER git failure (e.g. a bad branch name; no merge
    # started, nothing to abort). We key on unmerged paths, NOT git's English "CONFLICT" text,
    # so this is locale-independent and never calls --abort when no merge is underway.
    unmerged = sorted(
        _git(repo, "diff", "--name-only", "--diff-filter=U", allow_fail=True)
        .stdout.strip().splitlines()
    )
    if not unmerged:
        raise GitError(
            f"git merge {branch!r} into {into!r} failed (not a conflict): {result.stderr.strip()}"
        )

    # Conflicts == disagreement: abort to leave the repo clean, surface the contested files.
    _git(repo, "merge", "--abort")
    return {
        "merged": False,
        "conflicts": unmerged,
        "commit": None,
    }


def revert_claim(repo: pathlib.Path | str, commit_sha: str) -> str:
    """Revert a specific commit on the current branch.

    Args:
        repo: Repository path.
        commit_sha: Full or abbreviated SHA of the commit to revert.

    Returns:
        SHA of the newly created revert commit.

    Raises GitError if the revert fails (e.g., commit not found, tree conflict).
    """
    _git(repo, "revert", "--no-edit", commit_sha)
    return head_sha(repo)
