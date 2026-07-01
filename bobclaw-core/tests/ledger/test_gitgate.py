import pytest
import subprocess
import json
import os
from pathlib import Path
from core.ledger.gitgate import run_merge_gate
from core.ledger.gitdag import (
    normalize_slug,
    commit_trajectory,
    branch_run,
    current_branch,
    head_sha,
    is_clean,
    merge_synthesis,
    revert_claim,
    GitError,
)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def test_normalize_slug():
    assert normalize_slug("Iran War & Oil!") == "iran-war-oil"
    assert normalize_slug("  Hello   World!!  ") == "hello-world"
    assert normalize_slug("---a---") == "a"
    assert normalize_slug("NFKC_Test é") == "nfkc-test-e"
    assert normalize_slug("") == ""
    # idempotent
    assert normalize_slug("iran-war-oil") == "iran-war-oil"


def test_branch_run_and_current_branch(git_repo):
    branch = branch_run(git_repo, "test-slug", date="20250101")
    assert branch == "research/20250101-test-slug"
    assert current_branch(git_repo) == branch


def test_commit_trajectory_creates_commit(git_repo):
    # Write a change inside ledger/
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "a") as f:
        f.write('{"test":1}\n')
    sha = commit_trajectory(git_repo, "test commit")
    assert sha is not None
    assert len(sha) == 40
    # Verify commit exists
    result = _git(git_repo, "rev-parse", sha)
    assert result.stdout.strip() == sha
    # Clean status
    assert is_clean(git_repo)


def test_commit_trajectory_returns_none_on_no_change(git_repo):
    sha = commit_trajectory(git_repo, "noop")
    assert sha is None


def test_commit_trajectory_raises_on_tool_call(git_repo):
    with pytest.raises(GitError):
        commit_trajectory(git_repo, "tool call", boundary_kind="TOOL_CALL")


def test_merge_synthesis_clean(git_repo):
    # Create a divergent branch with a change
    branch_run(git_repo, "feature", date="20250101")
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "a") as f:
        f.write('{"feature":1}\n')
    commit_trajectory(git_repo, "feature commit")
    # Switch back to main
    _git(git_repo, "checkout", "main")
    result = merge_synthesis(git_repo, "research/20250101-feature")
    assert result["merged"] is True
    assert result["conflicts"] == []
    assert len(result["commit"]) == 40
    assert is_clean(git_repo)
    assert current_branch(git_repo) == "main"


def test_merge_synthesis_conflict_and_cleanup(git_repo):
    # Create base commit on main with a specific line
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "w") as f:
        f.write('{"common": "base"}\n')
    commit_trajectory(git_repo, "base commit")
    base_sha = head_sha(git_repo)

    # Create branch and change the same line
    branch_run(git_repo, "branch-a", date="20250101")
    with open(ledger_file, "w") as f:
        f.write('{"common": "branch-a"}\n')
    commit_trajectory(git_repo, "branch-a change")

    # Back to main, change same line differently
    _git(git_repo, "checkout", "main")
    with open(ledger_file, "w") as f:
        f.write('{"common": "main-change"}\n')
    commit_trajectory(git_repo, "main change")

    # Now try merge
    result = merge_synthesis(git_repo, "research/20250101-branch-a")
    assert result["merged"] is False
    assert len(result["conflicts"]) > 0
    # The conflict must be in ledger/events.jsonl
    assert any("events.jsonl" in p for p in result["conflicts"])
    # After abort, repo is clean and still on main
    assert is_clean(git_repo)
    assert current_branch(git_repo) == "main"
    # File content should be the original main version (no merge markers)
    with open(ledger_file) as f:
        content = f.read()
    assert '{"common": "main-change"}' in content


def test_revert_claim(git_repo):
    # Create a commit to revert
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "a") as f:
        f.write('{"revertable": 1}\n')
    sha_to_revert = commit_trajectory(git_repo, "commit to revert")
    original_head = head_sha(git_repo)
    revert_sha = revert_claim(git_repo, sha_to_revert)
    assert revert_sha != original_head
    # Check that the change is gone
    result = _git(git_repo, "show", revert_sha, "--stat")
    assert "events.jsonl" in result.stdout
    # File content should not contain the reverted line
    with open(ledger_file) as f:
        content = f.read()
    assert '{"revertable": 1}' not in content


def test_run_merge_gate_merged(git_repo):
    # Create a branch with a clean change and all verified verdicts
    branch = branch_run(git_repo, "feature", date="20250101")
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "a") as f:
        f.write('{"clean":1}\n')
    commit_trajectory(git_repo, "feature commit")
    # Back to main
    _git(git_repo, "checkout", "main")
    verdicts = [
        {"bid_key": "bid1", "verified": True, "exhausted": False},
        {"bid_key": "bid2", "verified": True, "exhausted": False},
    ]
    result = run_merge_gate(git_repo, branch, verdicts)
    assert result["decision"] == "FAST_FORWARD"
    assert result["action"] == "merged"
    assert result["merge_result"]["merged"] is True
    assert len(result["merge_result"]["commit"]) == 40
    assert result["merge_result"]["conflicts"] == []
    # Merged commit should be on main
    assert current_branch(git_repo) == "main"


def test_run_merge_gate_reverted(git_repo):
    # Create a simple branch (no conflict needed)
    branch = branch_run(git_repo, "failing-claim", date="20250101")
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "a") as f:
        f.write('{"failing":1}\n')
    commit_trajectory(git_repo, "failing commit")
    _git(git_repo, "checkout", "main")

    verdicts = [
        {"bid_key": "bid_fail", "verified": False, "exhausted": False},
        {"bid_key": "bid_ok", "verified": True, "exhausted": False},
    ]
    erg_entries = {
        "bid_fail": {
            "bid_key": "bid_fail",
            "retry_count": 1,
            "tried_sources": [],
            "status": "PENDING",
        }
    }
    result = run_merge_gate(
        git_repo, branch, verdicts, erg_entries=erg_entries
    )
    assert result["decision"] == "REVERT"
    assert result["action"] == "reverted"
    assert len(result["erg_directives"]) == 1
    directive = result["erg_directives"][0]
    assert "directive" in directive
    # The directive should come from on_entailment_failure (e.g. re-branch)
    # Ensure repo is clean and still on main (no merge happened)
    assert is_clean(git_repo)
    assert current_branch(git_repo) == "main"


def test_run_merge_gate_escalated(git_repo):
    verdicts = [
        {"bid_key": "bid1", "verified": True, "exhausted": False},
    ]
    result = run_merge_gate(
        git_repo, "some-branch", verdicts, budget_escalated=True
    )
    assert result["decision"] == "ESCALATE"
    assert result["action"] == "escalate"
    assert "reasons" in result


def test_run_merge_gate_conflict(git_repo):
    # Set up a conflicting branch
    # Base commit
    ledger_file = Path(git_repo) / "ledger" / "events.jsonl"
    with open(ledger_file, "w") as f:
        f.write('{"conflict": "base"}\n')
    commit_trajectory(git_repo, "base commit")

    # Create branch and change line
    branch = branch_run(git_repo, "conflict-branch", date="20250101")
    with open(ledger_file, "w") as f:
        f.write('{"conflict": "branch"}\n')
    commit_trajectory(git_repo, "branch change")
    _git(git_repo, "checkout", "main")
    with open(ledger_file, "w") as f:
        f.write('{"conflict": "main"}\n')
    commit_trajectory(git_repo, "main change")

    verdicts = [
        {"bid_key": "bid1", "verified": True, "exhausted": False},
    ]
    result = run_merge_gate(git_repo, branch, verdicts)
    assert result["decision"] == "FAST_FORWARD"
    assert result["action"] == "conflict"
    assert result["merge_result"]["merged"] is False
    assert len(result["merge_result"]["conflicts"]) > 0
    # Repo should be clean and on main
    assert is_clean(git_repo)
    assert current_branch(git_repo) == "main"
