import subprocess
import json
import pytest
from core.ledger.gitlog import blame_claim, render_decision_log
from core.ledger.gitdag import GitError


def _git(repo, *args):
    """Helper for test arrangement using subprocess directly."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )


def _append_event(repo, event, commit_message):
    """Append a JSON line to ledger/events.jsonl and commit it."""
    path = repo / "ledger" / "events.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    _git(repo, "add", str(path))
    _git(repo, "commit", "-m", commit_message)


def test_blame_claim_returns_targeted_events(git_repo):
    repo = git_repo  # pathlib.Path or string? conftest gives str, but we can use Path from pathlib? We'll assume str, use Path for file operations.
    from pathlib import Path
    repo = Path(repo)
    # Create three events: two targeting "Cx", one targeting "Cy"
    events = [
        {"id": "ev1", "targets": [{"claim": "Cx", "polarity": "corroborate"}]},
        {"id": "ev2", "targets": [{"claim": "Cy", "polarity": "corroborate"}]},
        {"id": "ev3", "targets": [{"claim": "Cx", "polarity": "corroborate"}]},
    ]
    _append_event(repo, events[0], "first event for Cx")
    sha_ev1 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _append_event(repo, events[1], "event for Cy")
    _append_event(repo, events[2], "second event for Cx")
    sha_ev3 = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = blame_claim(str(repo), "Cx")
    # Expect exactly two items, in file order (ev1 then ev3)
    assert len(result) == 2
    assert result[0]["event_id"] == "ev1"
    assert result[1]["event_id"] == "ev3"
    # multi-event attribution: each event maps to the EXACT commit that introduced it (audit-r3 S5 —
    # a constant dummy sha must NOT pass).
    assert result[0]["commit"] == sha_ev1
    assert result[1]["commit"] == sha_ev3
    assert sha_ev1 != sha_ev3
    for entry in result:
        assert len(entry["commit"]) == 40
        assert isinstance(entry["commit"], str)
        assert entry["author"]  # non-empty
        assert len(entry["date"]) == 10
        # date format YYYY-MM-DD
        assert entry["date"][4] == "-" and entry["date"][7] == "-"

    # Unknown claim returns []
    assert blame_claim(str(repo), "Cz") == []


def test_blame_claim_no_events(git_repo):
    repo = git_repo  # str
    assert blame_claim(repo, "NONEXIST") == []


def test_render_decision_log_returns_markdown_and_writes_file(git_repo):
    from pathlib import Path
    repo = Path(git_repo)
    # Add a couple of commits with distinct subjects
    _append_event(repo, {"id": "e1", "targets": [{"claim": "C1"}]}, "First subject")
    _append_event(repo, {"id": "e2", "targets": [{"claim": "C2"}]}, "Second subject")

    output_path = repo / "decision-log" / "experiment-log.md"
    # Ensure it doesn't exist before
    output_path.unlink(missing_ok=True)

    markdown = render_decision_log(str(repo), write=True)
    # Returned string contains the header and commit subjects
    assert "# Decision log (derived from the ledger commit DAG)" in markdown
    assert "First subject" in markdown
    assert "Second subject" in markdown
    # Every commit heading is well-formed: "## <7-hex> — <date> — <subject>" on ONE line, no
    # stray leading newline glued onto the sha (git separates records with a newline).
    headings = [l for l in markdown.splitlines() if l.startswith("## ")]
    # 3 commits rendered: the fixture's "init ledger" + our two (none swallowed by a broken split).
    assert len(headings) == 3
    import re as _re
    for h in headings:
        assert _re.match(r"## [0-9a-f]{7} — \d{4}-\d{2}-\d{2} — ", h), h
    assert any(h.endswith("First subject") for h in headings)
    assert any(h.endswith("Second subject") for h in headings)
    # File exists and matches
    assert output_path.exists()
    with output_path.open("r", encoding="utf-8") as f:
        assert f.read() == markdown

    # Verify git status shows only the untracked file (or nothing else changed)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout
    # Expect only the untracked decision-log/ directory
    lines = [l for l in status.splitlines() if l.strip()]
    assert all(l.startswith("?? ") for l in lines)
    assert any("decision-log" in l for l in lines)


def test_render_decision_log_empty_scope(git_repo):
    from pathlib import Path
    repo = Path(git_repo)
    # Add one commit so we have something to compare
    _append_event(repo, {"id": "e1", "targets": [{"claim": "C1"}]}, "Some commit")
    # Use a range that contains no commits: HEAD^..HEAD^ (if HEAD^ exists)
    # We need to get the parent of HEAD (initial commit)
    parent = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^"],
        capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()
    if not parent:
        # No parent (only one commit) – skip empty test; we can use HEAD..HEAD
        empty_range = "HEAD..HEAD"
    else:
        empty_range = f"{parent}..{parent}"
    markdown = render_decision_log(str(repo), ref=empty_range, write=False)
    assert "# Decision log" in markdown
    assert "_(no commits)_" in markdown


def test_render_decision_log_option_injection_guard(git_repo):
    repo = git_repo
    with pytest.raises(GitError):
        render_decision_log(repo, ref="--output=/etc/x")


def test_render_decision_log_body_with_separator_bytes(git_repo):
    """Commit bodies may legally contain \x1f/\x1e; NUL parsing must not split them."""
    from pathlib import Path
    repo = Path(git_repo)

    # Body that embeds both the old field and record separators.
    body_with_separators = "Body contains \x1f field-sep and \x1e record-sep inside."
    _append_event(repo, {"id": "e1", "targets": [{"claim": "C1"}]}, "First subject")
    _append_event(
        repo,
        {"id": "e2", "targets": [{"claim": "C2"}]},
        "Second subject\n\n" + body_with_separators,
    )

    markdown = render_decision_log(str(repo), write=False)
    headings = [l for l in markdown.splitlines() if l.startswith("## ")]
    # init ledger + first + second = 3 commits; none swallowed by a bad split.
    assert len(headings) == 3
    assert "Second subject" in markdown
    # Full body must survive intact.
    assert body_with_separators in markdown


# --- audit-round-1 convergence: pin the SHA attribution + the injection guards (Section 5) ---

def test_blame_claim_attributes_the_introducing_commit(git_repo):
    """blame must return the EXACT commit that introduced the event, not just any 40-hex string
    (a dummy sha would pass a length-only check)."""
    from pathlib import Path
    repo = Path(git_repo)
    _append_event(repo, {"id": "ev1", "targets": [{"claim": "Cx"}]}, "introduces ev1")
    sha_ev1 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # a later, unrelated commit so HEAD != the introducing commit
    _append_event(repo, {"id": "ev2", "targets": [{"claim": "Cy"}]}, "later commit")
    result = blame_claim(str(repo), "Cx")
    assert len(result) == 1
    assert result[0]["event_id"] == "ev1"
    assert result[0]["commit"] == sha_ev1  # the INTRODUCING commit, not HEAD


def test_blame_claim_events_path_injection_guard(git_repo):
    """A leading-dash events_path must fail closed (option injection)."""
    with pytest.raises(GitError):
        blame_claim(git_repo, "Cx", events_path="--output=/etc/x")


def test_render_decision_log_paths_injection_guard(git_repo):
    """A leading-dash entry in paths must fail closed (option injection)."""
    with pytest.raises(GitError):
        render_decision_log(git_repo, paths=["--output=/etc/x"])


def test_render_decision_log_order_and_real_sha(git_repo):
    """Headings are newest-first AND each short sha is a real prefix of an actual commit
    (a dummy constant like 'deadbeef' would NOT pass — audit-r2 S5.1/S5.2)."""
    from pathlib import Path
    repo = Path(git_repo)
    _append_event(repo, {"id": "e1", "targets": [{"claim": "C1"}]}, "Older subject")
    older = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _append_event(repo, {"id": "e2", "targets": [{"claim": "C2"}]}, "Newer subject")
    newer = _git(repo, "rev-parse", "HEAD").stdout.strip()

    md = render_decision_log(str(repo), write=False)
    headings = [l for l in md.splitlines() if l.startswith("## ")]
    # newest-first: "Newer subject" appears before "Older subject".
    idx_new = next(i for i, h in enumerate(headings) if h.endswith("Newer subject"))
    idx_old = next(i for i, h in enumerate(headings) if h.endswith("Older subject"))
    assert idx_new < idx_old
    # each rendered short sha is a real prefix of an existing full commit sha.
    full_shas = _git(repo, "rev-list", "HEAD").stdout.split()
    for h in headings:
        short = h.split(" ", 2)[1]  # "## <short> — ..."
        assert any(s.startswith(short) for s in full_shas), short
    # the two we committed are present by their real short shas.
    assert any(newer.startswith(h.split(" ", 2)[1]) for h in headings if h.endswith("Newer subject"))
    assert any(older.startswith(h.split(" ", 2)[1]) for h in headings if h.endswith("Older subject"))
