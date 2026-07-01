from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.ledger.session import (
    build_provenance_trailers,
    commit_trajectory_with_provenance,
    ledger_slice,
    provenance_from_transcript,
)
from core.ledger.gitdag import GitError


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _git(repo: str, *args: str, allow_fail: bool = False) -> subprocess.CompletedProcess:
    """Thin wrapper around the single subprocess site (as per contract)."""
    cmd = ["git", "-C", repo, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 and not allow_fail:
        raise GitError(f"git error: {result.stderr.strip()}")
    return result


def _append_event(repo: str, event_id: str, claim_id: str, *, to_events_path: str = "ledger/events.jsonl"):
    """Append a single event to the events file and stage it (no commit)."""
    events_file = Path(repo) / to_events_path
    events_file.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "id": event_id,
        "targets": [{"claim": claim_id, "polarity": "corroborate"}],
    }
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    _git(repo, "add", to_events_path)


def _commit(repo: str, message: str):
    """Create a commit (assumes staged changes). Returns hex sha."""
    out = _git(repo, "commit", "-m", message)
    # Retrieve the new sha
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return sha


# ---------------------------------------------------------------------------
# ledger_slice tests
# ---------------------------------------------------------------------------

def test_ledger_slice_two_events(git_repo: str):
    """Two commits adding two events targeting Cx -> event_count==2, claims includes Cx."""
    events_path = "ledger/events.jsonl"
    # first commit
    _append_event(git_repo, "ev1", "Cx", to_events_path=events_path)
    sha1 = _commit(git_repo, "first event")
    # second commit
    _append_event(git_repo, "ev2", "Cx", to_events_path=events_path)
    sha2 = _commit(git_repo, "second event")
    # range: first..second (parent of first is main initial commit)
    # we need to get the initial commit sha to build the range
    initial_sha = _git(git_repo, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    commit_range = f"{initial_sha}..{sha2}"
    result = ledger_slice(git_repo, commit_range, events_path=events_path)
    assert result["event_count"] == 2
    event_ids = [ev["id"] for ev in result["events"]]
    assert "ev1" in event_ids
    assert "ev2" in event_ids
    assert "Cx" in result["claims"]
    # git A..B is exclusive of A: initial_sha..sha2 == {sha1, sha2} -> 2 commits (rev-list newest-first).
    assert len(result["commits"]) == 2
    assert result["commits"][0] == sha2  # newest-first
    assert result["branch"] == "main"


def test_ledger_slice_empty_range(git_repo: str):
    """HEAD..HEAD -> event_count==0, events==[]."""
    result = ledger_slice(git_repo, "HEAD..HEAD")
    assert result["event_count"] == 0
    assert result["events"] == []


def test_ledger_slice_leading_dash_raises(git_repo: str):
    """Guard against option injection in commit_range."""
    with pytest.raises(GitError):
        ledger_slice(git_repo, "--bad")


# ---------------------------------------------------------------------------
# build_provenance_trailers tests
# ---------------------------------------------------------------------------

def test_build_provenance_trailers_full(git_repo: str):
    """All five arguments -> five lines in fixed order."""
    trailers = build_provenance_trailers(
        git_branch="main",
        cwd="/home/user",
        version="1.0",
        session_id="sess-1",
        conversation_id="conv-1",
    )
    assert len(trailers) == 5
    assert trailers[0] == "Ledger-Branch: main"
    assert trailers[1] == "Ledger-Cwd: /home/user"
    assert trailers[2] == "Ledger-Version: 1.0"
    assert trailers[3] == "Session-Id: sess-1"
    assert trailers[4] == "Conversation-Id: conv-1"


def test_build_provenance_trailers_partial(git_repo: str):
    """Only a subset provided -> only those lines."""
    trailers = build_provenance_trailers(git_branch="develop", session_id="sess-2")
    assert len(trailers) == 2
    assert trailers[0] == "Ledger-Branch: develop"
    assert trailers[1] == "Session-Id: sess-2"


def test_build_provenance_trailers_none(git_repo: str):
    """No arguments -> empty list."""
    assert build_provenance_trailers() == []


def test_build_provenance_trailers_collapse_newline(git_repo: str):
    """Newlines in value are collapsed to a single space, no forged trailers."""
    val = "line1\nLine2"
    trailers = build_provenance_trailers(git_branch=val)
    assert len(trailers) == 1
    assert trailers[0] == "Ledger-Branch: line1 Line2"


def test_build_provenance_trailers_collapse_crlf_and_blank_lines(git_repo: str):
    """\\r\\n and consecutive newlines must ALL collapse — a value can never forge a 2nd trailer
    (audit-r2 S5.4: single-\\n was the only case covered)."""
    # CRLF: \r and \n each -> a space (two spaces here), still ONE trailer line.
    crlf = build_provenance_trailers(session_id="a\r\nEvil: injected")
    assert crlf == ["Session-Id: a  Evil: injected"]
    assert len(crlf) == 1  # NOT a forged "Evil:" trailer
    # consecutive newlines.
    multi = build_provenance_trailers(cwd="a\n\nb")
    assert multi == ["Ledger-Cwd: a  b"]


# ---------------------------------------------------------------------------
# provenance_from_transcript tests
# ---------------------------------------------------------------------------

def test_provenance_from_transcript_full(git_repo: str):
    """Object with all five attrs -> dict of non-empty values."""
    tx = SimpleNamespace(
        git_branch="main",
        cwd="/tmp",
        version="2.0",
        session_id="sess-3",
        conversation_id="conv-3",
    )
    result = provenance_from_transcript(tx)
    assert result == {
        "git_branch": "main",
        "cwd": "/tmp",
        "version": "2.0",
        "session_id": "sess-3",
        "conversation_id": "conv-3",
    }


def test_provenance_from_transcript_empty_attrs(git_repo: str):
    """Object with empty string attrs -> only those non-empty."""
    tx = SimpleNamespace(
        git_branch="",
        cwd="/home",
        version="",
        session_id="",
        conversation_id="conv-4",
    )
    result = provenance_from_transcript(tx)
    assert result == {"cwd": "/home", "conversation_id": "conv-4"}


def test_provenance_from_transcript_missing_attrs(git_repo: str):
    """Object missing attrs -> empty dict."""
    tx = SimpleNamespace(foo="bar")  # has none of the expected attrs
    result = provenance_from_transcript(tx)
    assert result == {}


# ---------------------------------------------------------------------------
# commit_trajectory_with_provenance tests
# ---------------------------------------------------------------------------

def test_commit_trajectory_with_provenance_trailers(git_repo: str):
    """Trailers are appended to the message body."""
    # Stage a change first
    (Path(git_repo) / "ledger/events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with open(Path(git_repo) / "ledger/events.jsonl", "a") as f:
        f.write('{"id":"t1","targets":[{"claim":"Cx","polarity":"corroborate"}]}\n')
    _git(git_repo, "add", "ledger/events.jsonl")
    trailers = build_provenance_trailers(git_branch="feature", session_id="sess-5")
    sha = commit_trajectory_with_provenance(
        git_repo,
        "Trailer test",
        trailers=trailers,
        paths=["ledger/events.jsonl"],
    )
    assert isinstance(sha, str) and len(sha) == 40
    # check commit message body contains trailer lines
    log = _git(git_repo, "log", "-1", "--format=%B").stdout.strip()
    assert "Ledger-Branch: feature" in log
    assert "Session-Id: sess-5" in log
    assert log.startswith("Trailer test\n\n")


def test_commit_trajectory_with_provenance_no_trailers(git_repo: str):
    """trailers=None -> same as plain commit_trajectory (no trailer lines)."""
    # Stage a change first
    (Path(git_repo) / "ledger/events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with open(Path(git_repo) / "ledger/events.jsonl", "a") as f:
        f.write('{"id":"t2","targets":[{"claim":"Cy","polarity":"corroborate"}]}\n')
    _git(git_repo, "add", "ledger/events.jsonl")
    sha = commit_trajectory_with_provenance(
        git_repo,
        "No trailers",
        trailers=None,
        paths=["ledger/events.jsonl"],
    )
    assert isinstance(sha, str) and len(sha) == 40
    log = _git(git_repo, "log", "-1", "--format=%B").stdout.strip()
    assert log == "No trailers"


def test_commit_trajectory_with_provenance_tool_call_guard(git_repo: str):
    """boundary_kind='TOOL_CALL' raises GitError."""
    # Stage a change first
    (Path(git_repo) / "ledger/events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with open(Path(git_repo) / "ledger/events.jsonl", "a") as f:
        f.write('{"id":"t3","targets":[{"claim":"Cz","polarity":"corroborate"}]}\n')
    _git(git_repo, "add", "ledger/events.jsonl")
    with pytest.raises(GitError):
        commit_trajectory_with_provenance(
            git_repo,
            "Tool call guard",
            trailers=None,
            paths=["ledger/events.jsonl"],
            boundary_kind="TOOL_CALL",
        )


def test_commit_trajectory_with_provenance_no_staged(git_repo: str):
    """No staged changes -> returns None."""
    result = commit_trajectory_with_provenance(
        git_repo,
        "No op",
        trailers=None,
        paths=["ledger/events.jsonl"],
    )
    assert result is None


# --- audit-round-2 convergence ---

def test_commit_trajectory_with_provenance_empty_trailers_is_bare(git_repo: str):
    """trailers=[] (e.g. build_provenance_trailers() with no provenance) behaves like None:
    a BARE message with no dangling blank lines (audit-r2 S4.2)."""
    (Path(git_repo) / "ledger/events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    with open(Path(git_repo) / "ledger/events.jsonl", "a") as f:
        f.write('{"id":"te","targets":[{"claim":"Cx"}]}\n')
    _git(git_repo, "add", "ledger/events.jsonl")
    sha = commit_trajectory_with_provenance(
        git_repo, "Bare please", trailers=[], paths=["ledger/events.jsonl"])
    assert isinstance(sha, str) and len(sha) == 40
    body = _git(git_repo, "log", "-1", "--format=%B").stdout.strip()
    assert body == "Bare please"  # no trailing blank lines / footer


def test_ledger_slice_events_path_injection_guard(git_repo: str):
    """A leading-dash events_path fails closed (audit-r2 S3)."""
    with pytest.raises(GitError):
        ledger_slice(git_repo, "HEAD~1..HEAD", events_path="--output=/etc/x")


def test_commit_trajectory_with_provenance_paths_injection_guard(git_repo: str):
    """A leading-dash path fails closed (audit-r2 S4.1)."""
    with pytest.raises(GitError):
        commit_trajectory_with_provenance(
            git_repo, "guard", trailers=None, paths=["--output=/etc/x"])
