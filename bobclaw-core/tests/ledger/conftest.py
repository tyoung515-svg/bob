"""Shared fixtures for the ledger git-DAG (Stage 2a) tests. A real temp git repo with a
minimal tracked ledger + an initial commit on `main`."""
from __future__ import annotations

import subprocess

import pytest


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway git repo at tmp_path/repo: branch `main`, one tracked `ledger/events.jsonl`
    and an initial commit. Returns the repo path as a str."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Ledger Test")
    led = repo / "ledger"
    led.mkdir()
    (led / "events.jsonl").write_text('{"id": "E00a", "statement": "seed"}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init ledger")
    _git(repo, "branch", "-M", "main")
    return str(repo)
