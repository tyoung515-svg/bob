import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from core.harness.session import LedgerSession


def _git(*args, cwd):
    """Run a git command in the given working directory, raise on failure."""
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_temp_git_repo():
    """Create a temporary directory with a git repo and return its path."""
    tmpdir = tempfile.mkdtemp()
    _git("init", cwd=tmpdir)
    _git("config", "user.email", "test@bobclaw.io", cwd=tmpdir)
    _git("config", "user.name", "Test", cwd=tmpdir)

    # Create initial events.jsonl with E0, and a claim file
    ledger_dir = os.path.join(tmpdir, "ledger")
    os.makedirs(os.path.join(ledger_dir, "claims"))
    events_path = os.path.join(ledger_dir, "events.jsonl")
    with open(events_path, "w") as f:
        f.write(json.dumps({"id": "E0", "value": "zero"}) + "\n")
    claim_path = os.path.join(ledger_dir, "claims", "C0.json")
    with open(claim_path, "w") as f:
        json.dump({"id": "C0", "statement": "first claim"}, f)

    _git("add", ".", cwd=tmpdir)
    _git("commit", "-m", "initial commit", cwd=tmpdir)
    _git("branch", "-M", "main", cwd=tmpdir)

    # Record first commit sha
    first_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmpdir, capture_output=True, text=True, check=True
    ).stdout.strip()

    # Second commit: append E1 (referencing claim C0 via targets, so ledger_slice
    # surfaces C0 in its claim-id list — slice claims are the ids events point at).
    with open(events_path, "a") as f:
        f.write(json.dumps({"id": "E1", "value": "one",
                            "targets": [{"claim": "C0", "polarity": "corroborate"}]}) + "\n")

    _git("add", ".", cwd=tmpdir)
    _git("commit", "-m", "second commit", cwd=tmpdir)

    return tmpdir, first_sha


class TestLedgerSession:
    """Tests for LedgerSession using a real temp git repo."""

    def test_slice_and_committed_ids(self):
        repo_path, first_sha = _init_temp_git_repo()
        session = LedgerSession(repo_path)
        commit_range = f"{first_sha}..HEAD"
        slc = session.slice(commit_range)
        assert slc["event_count"] == 1
        assert len(slc["events"]) == 1
        assert slc["events"][0]["id"] == "E1"
        # committed_ids should match
        ids = session.committed_ids(commit_range)
        assert ids == {"E1"}
        # ledger_slice.claims is a sorted LIST of claim ids referenced by the range's
        # event targets (E1 -> C0), NOT the read_ledger_at id->claim dict.
        assert "C0" in slc["claims"]
        # the full claim body lives in the read_ledger_at truth (a dict id->claim).
        assert session.truth_at("HEAD")["claims"]["C0"]["statement"] == "first claim"

    def test_truth_at_head(self):
        repo_path, _ = _init_temp_git_repo()
        session = LedgerSession(repo_path)
        truth = session.truth_at("HEAD")
        # Should contain both events and claims from head
        assert "E0" in {e["id"] for e in truth["events"]}
        assert "E1" in {e["id"] for e in truth["events"]}
        assert "C0" in truth["claims"]
        assert truth["claims"]["C0"]["statement"] == "first claim"

    def test_truth_at_older_ref(self):
        repo_path, first_sha = _init_temp_git_repo()
        session = LedgerSession(repo_path)
        # truth at first commit should have E0 but not E1
        old_truth = session.truth_at(first_sha)
        event_ids = {e["id"] for e in old_truth["events"]}
        assert "E0" in event_ids
        assert "E1" not in event_ids
        # claims still present
        assert "C0" in old_truth["claims"]

    def test_empty_range_returns_empty_set(self):
        repo_path, _ = _init_temp_git_repo()
        session = LedgerSession(repo_path)
        empty_ids = session.committed_ids("HEAD..HEAD")
        assert empty_ids == set()

    def test_claims_in_slice_and_truth(self):
        repo_path, first_sha = _init_temp_git_repo()
        session = LedgerSession(repo_path)
        # full range from root to HEAD should include both events and claims
        full_slice = session.slice(f"{first_sha}..HEAD")
        assert full_slice["event_count"] == 1  # only the delta (E1)
        # but truth_at root should have E0 and claims
        root_truth = session.truth_at(first_sha)
        assert root_truth["events"][0]["id"] == "E0"
        assert "C0" in root_truth["claims"]
