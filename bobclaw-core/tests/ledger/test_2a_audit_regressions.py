"""Regression tests for the Stage-2a round-1 fleet diff-audit findings.
See tasks/2026-06-29-lks-git-dag-ledger/audit_2a_result_r1.txt."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.ledger.gitdag import (
    GitError,
    branch_run,
    commit_trajectory,
    head_sha,
    merge_synthesis,
    normalize_slug,
)
from core.ledger.gitgate import run_merge_gate


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


# ── audit §3.1 — branch_run must reject a ref-unsafe `date` ──────────────────────
def test_branch_run_rejects_unsafe_date(git_repo):
    with pytest.raises(GitError):
        branch_run(git_repo, "x", date="../evil")


# ── audit §3.4 — empty/punctuation slug falls back to "run" (no trailing-dash ref) ─
def test_branch_run_empty_slug_fallback(git_repo):
    assert branch_run(git_repo, "!!!", date="20260629") == "research/20260629-run"
    # normalize_slug itself stays a pure normalizer
    assert normalize_slug("!!!") == ""


# ── audit §1 — merge_synthesis raises on a NON-conflict failure (no spurious abort) ─
def test_merge_synthesis_bad_branch_raises(git_repo):
    with pytest.raises(GitError):
        merge_synthesis(git_repo, "no-such-branch", into="main")


# ── audit §1 — a real conflict still returns the conflicts list + leaves repo clean ─
def test_merge_synthesis_conflict_aborts_clean(git_repo):
    repo = git_repo
    ev = Path(repo) / "ledger" / "events.jsonl"
    # branch edits the seed line one way...
    b = branch_run(repo, "x", date="20260629")
    ev.write_text("BRANCH VERSION\n", encoding="utf-8")
    commit_trajectory(repo, "branch edit")
    # ...main edits the SAME line another way
    _git(repo, "checkout", "main")
    ev.write_text("MAIN VERSION\n", encoding="utf-8")
    commit_trajectory(repo, "main edit")
    res = merge_synthesis(repo, b, into="main")
    assert res["merged"] is False and res["conflicts"]
    assert subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip() == ""  # clean (aborted)


# ── audit §4 — REVERT means the branch's change is ABSENT from `into` ────────────
def test_revert_leaves_change_absent_from_into(git_repo):
    repo = git_repo
    ev = Path(repo) / "ledger" / "events.jsonl"
    b = branch_run(repo, "bad", date="20260629")
    ev.write_text(ev.read_text(encoding="utf-8") + '{"id": "BADX"}\n', encoding="utf-8")
    commit_trajectory(repo, "bad change")
    _git(repo, "checkout", "main")
    res = run_merge_gate(repo, b, [{"bid_key": "k", "verified": False, "exhausted": False}], into="main")
    assert res["action"] == "reverted"
    # the defining REVERT invariant: the bad change never reached `into`
    assert "BADX" not in ev.read_text(encoding="utf-8")


# ── audit r2 §4 — ESCALATE performs ZERO git work (state-change, not just shape) ──
def test_escalate_performs_no_git_work(git_repo):
    repo = git_repo
    ev = Path(repo) / "ledger" / "events.jsonl"
    b = branch_run(repo, "x", date="20260629")
    ev.write_text(ev.read_text(encoding="utf-8") + '{"id": "ESCX"}\n', encoding="utf-8")
    commit_trajectory(repo, "branch change")
    _git(repo, "checkout", "main")
    before = head_sha(repo)
    res = run_merge_gate(repo, b, [{"bid_key": "k", "verified": True, "exhausted": False}],
                         budget_escalated=True)
    assert res["action"] == "escalate"
    assert head_sha(repo) == before                       # no commit / merge happened
    assert "ESCX" not in ev.read_text(encoding="utf-8")   # branch change not on main


# ── audit r2 §4 — a CLEAN merge actually lands the branch content in `into` ──────
def test_clean_merge_brings_branch_content_into_main(git_repo):
    repo = git_repo
    ev = Path(repo) / "ledger" / "events.jsonl"
    b = branch_run(repo, "good", date="20260629")
    ev.write_text(ev.read_text(encoding="utf-8") + '{"id": "GOODX"}\n', encoding="utf-8")
    commit_trajectory(repo, "good change")
    _git(repo, "checkout", "main")
    res = run_merge_gate(repo, b, [{"bid_key": "k", "verified": True, "exhausted": False}], into="main")
    assert res["action"] == "merged"
    assert "GOODX" in ev.read_text(encoding="utf-8")      # content really merged into main
