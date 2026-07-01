from __future__ import annotations

import json
import re
from pathlib import Path

from core.ledger.gitdag import _git, GitError, current_branch, commit_trajectory


# ---------------------------------------------------------------------------
# L4 – ledger_slice: reconstruct events contributed by a commit range
# ---------------------------------------------------------------------------

def ledger_slice(
    repo: str | Path,
    commit_range: str,
    *,
    events_path: str = "ledger/events.jsonl",
) -> dict:
    """
    Return a dict describing the commits and events in *commit_range*.

    Guard against option injection: raises GitError if *commit_range* starts
    with ``-``.

    Returns::
        {
            "commit_range": str,
            "commits": list[str],       # SHAs newest-first, empty if no commits
            "events": list[dict],       # parsed event dicts with ``id``
            "event_count": int,
            "claims": list[str],        # sorted unique claim ids
            "branch": str,              # current branch name
        }
    """
    _guard_not_option(commit_range, "commit_range")
    # events_path is already after `--` in the git diff below, but guard it for consistency with
    # blame_claim and fail-fast clarity (mirrors the module-wide option-injection convention).
    _guard_not_option(events_path, "events_path")
    repo = Path(repo)

    # 1) Commit list (newest-first)
    rev_list = _git(repo, "rev-list", commit_range, allow_fail=True)
    commits = rev_list.stdout.strip().splitlines() if rev_list.returncode == 0 else []
    # (empty range -> returncode 128? Actually `git rev-list HEAD..HEAD` returns 0 with empty output.
    #  Safe: just splitlines.)

    # 2) Added events via git diff
    diff_out = _git(repo, "diff", commit_range, "--", events_path, allow_fail=True)
    events: list[dict] = []
    if diff_out.returncode == 0:
        for line in diff_out.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                stripped = line[1:]  # remove leading '+'
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and "id" in ev:
                    events.append(ev)

    # 3) Collect claim ids from targets
    claims: set[str] = set()
    for ev in events:
        for tgt in ev.get("targets", []):
            if isinstance(tgt, dict):
                claim = tgt.get("claim")
                if claim:
                    claims.add(claim)

    branch = current_branch(repo)

    return {
        "commit_range": commit_range,
        "commits": commits,
        "events": events,
        "event_count": len(events),
        "claims": sorted(claims),
        "branch": branch,
    }


# ---------------------------------------------------------------------------
# L5 – pure provenance helpers
# ---------------------------------------------------------------------------

_TRAILER_KEYS = [
    ("git_branch", "Ledger-Branch"),
    ("cwd", "Ledger-Cwd"),
    ("version", "Ledger-Version"),
    ("session_id", "Session-Id"),
    ("conversation_id", "Conversation-Id"),
]


def build_provenance_trailers(
    *,
    git_branch: str | None = None,
    cwd: str | None = None,
    version: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
) -> list[str]:
    """
    Return a list of git-trailer lines in the fixed order of ``_TRAILER_KEYS``.

    Only keys with a non‑empty stripped value are emitted. Every value is
    sanitised: ``\\r`` and ``\\n`` are replaced by a single space, then
    stripped — this prevents trailer injection via multiline values.
    """
    values = {k: v for k, v in locals().items() if v is not None}
    sanitised = {
        key: _sanitise_trailer_value(str(val))
        for key, val in values.items()
        if str(val).strip()
    }
    return [
        f"{trailer_key}: {sanitised[field_key]}"
        for field_key, trailer_key in _TRAILER_KEYS
        if field_key in sanitised
    ]


def _sanitise_trailer_value(value: str) -> str:
    """Replace all occurrences of \\r and \\n with a single space, then strip."""
    return value.replace("\r", " ").replace("\n", " ").strip()


def provenance_from_transcript(tx) -> dict:
    """
    Return a dict of non‑empty provenance fields extracted from *tx*.

    Duck‑typed access via ``getattr`` — does **not** import the ``lks``
    package. Only fields with a non‑empty string value are included.
    """
    result = {}
    for field_key, _ in _TRAILER_KEYS:
        val = getattr(tx, field_key, "")
        if val:  # non‑empty string (falsy values like 0 are not expected here)
            result[field_key] = val
    return result


# ---------------------------------------------------------------------------
# L5 writer – commit with provenance trailers via the locked delegate
# ---------------------------------------------------------------------------

def commit_trajectory_with_provenance(
    repo: str | Path,
    message: str,
    *,
    trailers: list[str] | None = None,
    paths: list[str] | None = None,
    boundary_kind: str = "ARTIFACT_COMPLETE",
) -> str | None:
    """
    Compose *message* with optional *trailers* and delegate to
    :func:`core.ledger.gitdag.commit_trajectory`.

    When *trailers* is ``None`` the behaviour is byte‑identical to a plain
    call of :func:`commit_trajectory` — no regression.
    """
    # Guard paths for consistency (commit_trajectory already stages them after `git add --`, so this
    # is fail-fast defense-in-depth, not the sole barrier).
    if paths:
        for p in paths:
            _guard_not_option(str(p), "path")
    # `if trailers:` (not `is not None`): an EMPTY trailer list behaves like None — a bare message
    # with no dangling blank lines. trailers=None stays byte-identical to a plain commit_trajectory.
    if trailers:
        full_message = message.rstrip() + "\n\n" + "\n".join(trailers)
    else:
        full_message = message

    return commit_trajectory(
        repo,
        full_message,
        paths=paths,
        boundary_kind=boundary_kind,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _guard_not_option(value: str, name: str) -> None:
    """Fail‑closed guard: any value starting with ``-`` raises GitError."""
    if value.startswith("-"):
        raise GitError(f"{name} must not start with '-': {value!r}")
