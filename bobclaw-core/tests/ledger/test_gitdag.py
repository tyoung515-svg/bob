import subprocess
import pytest
from pathlib import Path
from core.ledger.gitdag import (
    normalize_slug,
    commit_trajectory,
    branch_run,
    merge_synthesis,
    revert_claim,
    is_clean,
    current_branch,
    head_sha,
    GitError,
)
from core.ledger.gitgate import run_merge_gate
from core.ledger.erg import on_entailment_failure
from core.ledger.mergegate import merge_decision


# ---------------------------------------------------------------------------
# normalize_slug
# ---------------------------------------------------------------------------

class TestNormalizeSlug:
    def test_conversion(self):
        assert normalize_slug("Iran War & Oil!") == "iran-war-oil"

    def test_idempotent(self):
        slug = "iran-war-oil"
        assert normalize_slug(slug) == slug

    def test_collapse_repeats(self):
        assert normalize_slug("foo---bar__baz") == "foo-bar-baz"

    def test_leading_trailing_dashes_removed(self):
        assert normalize_slug("-hello-world-") == "hello-world"


# ---------------------------------------------------------------------------
# commit_trajectory
# ---------------------------------------------------------------------------

class TestCommitTrajectory:
    def test_first_commit_returns_sha(self, git_repo):
        repo = git_repo
        # Ensure there is an initial commit already (fixture does that).
        # Add a change to ledger/events.jsonl
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text('{"event": "test"}\n')
        sha = commit_trajectory(repo, "first trajectory", boundary_kind="ARTIFACT_COMPLETE")
        assert len(sha) == 40
        assert sha == head_sha(repo)

    def test_no_change_returns_none(self, git_repo):
        repo = git_repo
        # First commit to ensure clean state after a commit
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text('{"event": "first"}\n')
        sha = commit_trajectory(repo, "first", boundary_kind="ARTIFACT_COMPLETE")
        assert sha is not None
        # Second call with no changes
        result = commit_trajectory(repo, "second", boundary_kind="ARTIFACT_COMPLETE")
        assert result is None

    def test_tool_call_raises_giterror(self, git_repo):
        repo = git_repo
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text('{"event": "tool_test"}\n')
        with pytest.raises(GitError, match="not committable"):
            commit_trajectory(repo, "tool call", boundary_kind="TOOL_CALL")

    def test_only_default_paths_staged(self, git_repo):
        repo = git_repo
        # Create a file outside ledger/
        (Path(repo) / "outside.txt").write_text("outside\n")
        # Add content to ledger/events.jsonl
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text('{"event": "only_ledger"}\n')
        sha = commit_trajectory(repo, "default paths", boundary_kind="ARTIFACT_COMPLETE")
        assert sha is not None
        # Check that only ledger/ files are in the commit
        result = subprocess.run(
            ["git", "-C", repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
            capture_output=True, text=True, encoding="utf-8", check=True
        )
        files = result.stdout.strip().splitlines()
        assert all(f.startswith("ledger/") for f in files)


# ---------------------------------------------------------------------------
# branch_run
# ---------------------------------------------------------------------------

class TestBranchRun:
    def test_creates_and_checks_out_branch(self, git_repo):
        repo = git_repo
        date = "20250320"
        slug = "my-research"
        branch_name = branch_run(repo, slug, date=date, base="main")
        expected = f"research/{date}-{normalize_slug(slug)}"
        assert branch_name == expected
        assert current_branch(repo) == expected

    def test_current_branch_reflects_new_branch(self, git_repo):
        repo = git_repo
        branch_run(repo, "another", date="20250321", base="main")
        assert current_branch(repo) == "research/20250321-another"


# ---------------------------------------------------------------------------
# merge_synthesis
# ---------------------------------------------------------------------------

class TestMergeSynthesis:
    def test_clean_merge(self, git_repo):
        repo = git_repo
        # Create a branch and make a non-conflicting change
        events_path = Path(repo) / "ledger" / "events.jsonl"
        # Ensure main has initial content
        events_path.write_text("line1\n")
        commit_trajectory(repo, "main init", boundary_kind="ARTIFACT_COMPLETE")

        # Create branch
        branch_name = branch_run(repo, "clean-feature", date="20250320", base="main")
        events_path.write_text("line1\nline2\n")
        commit_trajectory(repo, "feature commit", boundary_kind="ARTIFACT_COMPLETE")

        # Merge back to main
        result = merge_synthesis(repo, branch_name, into="main")
        assert result["merged"] is True
        assert result["conflicts"] == []
        assert len(result["commit"]) == 40
        assert current_branch(repo) == "main"

    def test_conflicting_merge(self, git_repo):
        repo = git_repo
        events_path = Path(repo) / "ledger" / "events.jsonl"
        # Create a base main
        events_path.write_text("line A\nline B\nline C\n")
        commit_trajectory(repo, "base", boundary_kind="ARTIFACT_COMPLETE")

        # Create feature branch and change line B
        feat = branch_run(repo, "feat", date="20250320", base="main")
        events_path.write_text("line A\nline B modified by feat\nline C\n")
        commit_trajectory(repo, "feat change", boundary_kind="ARTIFACT_COMPLETE")

        # Switch back to main and change the same line differently
        subprocess.run(["git", "-C", repo, "checkout", "main"],
                       capture_output=True, text=True, check=True)
        events_path.write_text("line A\nline B modified by main\nline C\n")
        commit_trajectory(repo, "main change", boundary_kind="ARTIFACT_COMPLETE")

        # Attempt merge
        result = merge_synthesis(repo, feat, into="main")
        assert result["merged"] is False
        assert "ledger/events.jsonl" in result["conflicts"]
        # Repo should be clean and back on main
        assert is_clean(repo) is True
        assert current_branch(repo) == "main"


# ---------------------------------------------------------------------------
# revert_claim
# ---------------------------------------------------------------------------

class TestRevertClaim:
    def test_revert_changes_head_and_undoes_changes(self, git_repo):
        repo = git_repo
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text("original\n")
        commit_trajectory(repo, "original commit", boundary_kind="ARTIFACT_COMPLETE")
        original_sha = head_sha(repo)

        # Make a change
        events_path.write_text("changed\n")
        commit_trajectory(repo, "change to revert", boundary_kind="ARTIFACT_COMPLETE")
        changed_sha = head_sha(repo)

        assert changed_sha != original_sha

        # Revert the last commit
        revert_sha = revert_claim(repo, changed_sha)
        assert len(revert_sha) == 40
        # Check that file content is back to original
        assert events_path.read_text() == "original\n"
        # HEAD should be different from both original_sha and changed_sha
        current = head_sha(repo)
        assert current != original_sha
        assert current != changed_sha


# ---------------------------------------------------------------------------
# run_merge_gate (gitgate)
# ---------------------------------------------------------------------------

class TestRunMergeGate:
    def test_all_verified_merged(self, git_repo):
        repo = git_repo
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text("base\n")
        commit_trajectory(repo, "base", boundary_kind="ARTIFACT_COMPLETE")

        # Create feature branch with a change
        branch_name = branch_run(repo, "good-feature", date="20250320", base="main")
        events_path.write_text("base\nfeature change\n")
        commit_trajectory(repo, "feature commit", boundary_kind="ARTIFACT_COMPLETE")

        # Return to main and execute merge gate with all-verified verdicts
        subprocess.run(["git", "-C", repo, "checkout", "main"],
                       capture_output=True, text=True, check=True)

        verdicts = [
            {"bid_key": "bid1", "verified": True, "exhausted": False},
            {"bid_key": "bid2", "verified": True, "exhausted": False},
        ]
        result = run_merge_gate(repo, branch_name, verdicts, into="main")
        assert result["action"] == "merged"
        assert result["decision"] == "FAST_FORWARD"
        assert result["merge_result"]["merged"] is True
        assert "reasons" in result

    def test_failing_verdict_reverted(self, git_repo):
        repo = git_repo
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text("base\n")
        commit_trajectory(repo, "base", boundary_kind="ARTIFACT_COMPLETE")

        branch_name = branch_run(repo, "bad-feature", date="20250320", base="main")
        events_path.write_text("base\nbad change\n")
        commit_trajectory(repo, "bad commit", boundary_kind="ARTIFACT_COMPLETE")

        subprocess.run(["git", "-C", repo, "checkout", "main"],
                       capture_output=True, text=True, check=True)

        verdicts = [
            {"bid_key": "bid1", "verified": False, "exhausted": False},
            {"bid_key": "bid2", "verified": True, "exhausted": False},
        ]
        # Do not provide erg_entries; function will create defaults
        result = run_merge_gate(repo, branch_name, verdicts, into="main")
        assert result["action"] == "reverted"
        assert result["decision"] == "REVERT"
        # Should have one EG directive per failing verdict (bid1)
        assert len(result["erg_directives"]) == 1
        directive = result["erg_directives"][0]["directive"]
        # The directive from on_entailment_failure with retry_count<2 carries negative constraint signal
        # We don't need to check exact text, just presence
        assert "directive" in result["erg_directives"][0]
        assert "reasons" in result

    def test_budget_escalated_escalates(self, git_repo):
        repo = git_repo
        events_path = Path(repo) / "ledger" / "events.jsonl"
        events_path.write_text("base\n")
        commit_trajectory(repo, "base", boundary_kind="ARTIFACT_COMPLETE")

        branch_name = branch_run(repo, "escalate-feature", date="20250320", base="main")
        events_path.write_text("base\nsome change\n")
        commit_trajectory(repo, "commit", boundary_kind="ARTIFACT_COMPLETE")

        subprocess.run(["git", "-C", repo, "checkout", "main"],
                       capture_output=True, text=True, check=True)

        verdicts = [
            {"bid_key": "bid1", "verified": True, "exhausted": False},
        ]
        result = run_merge_gate(repo, branch_name, verdicts, into="main", budget_escalated=True)
        assert result["action"] == "escalate"
        assert result["decision"] == "ESCALATE"
        # No merge should have happened (HEAD unchanged)
        assert current_branch(repo) == "main"
        # Repo should be clean (no merge attempted)
        assert is_clean(repo) is True
