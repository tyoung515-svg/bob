import json
import os
import subprocess
import pytest

from core.ledger.project import read_ledger_at, projection_key, diff_ledger
from core.ledger.gitdag import GitError


# --- Helpers for arranging git state (only used inside tests) ---

def _git_run(repo, *args):
    """Run a git command in the repo and return the completed process.
    Not the shared _git helper (we are allowed direct subprocess for setup)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
        check=False, encoding="utf-8"
    )


def _write_file(repo, rel_path, content):
    """Write content to a file inside repo and ensure parent dirs exist."""
    full = os.path.join(repo, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)


def _git_add_commit(repo, message, *paths):
    """Add paths and create a commit."""
    for p in paths:
        _git_run(repo, "add", p).check_returncode()
    _git_run(repo, "commit", "-m", message).check_returncode()


# --- Tests for read_ledger_at ---

class TestReadLedgerAt:
    def test_reads_events_claims_falsifiers(self, git_repo):
        """After committing events, a claim, and a falsifiers file, read_ledger_at returns them."""
        repo = git_repo
        # Write events.jsonl
        events_content = (
            '{"id": "evt1", "targets": ["a"]}\n'
            '{"id": "evt2", "targets": ["b"]}\n'
        )
        _write_file(repo, "ledger/events.jsonl", events_content)
        # Write a claim file
        claim = {"id": "Cx", "title": "Test Claim", "statement": "some statement"}
        _write_file(repo, "ledger/claims/Cx.json", json.dumps(claim))
        # Write a falsifiers file — real falsifiers carry an "id" (F00a, ...), which read_ledger_at
        # filters on (same rule as events).
        falsifiers_content = '{"id": "F1"}\n{"id": "F2"}\n'
        _write_file(repo, "ledger/falsifiers.jsonl", falsifiers_content)
        _git_add_commit(repo, "initial setup", "ledger/events.jsonl", "ledger/claims/Cx.json", "ledger/falsifiers.jsonl")

        result = read_ledger_at(repo)
        assert result["ref"] == _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        assert len(result["events"]) == 2
        assert result["events"][0]["id"] == "evt1"
        assert result["events"][1]["id"] == "evt2"
        assert "Cx" in result["claims"]
        assert result["claims"]["Cx"]["statement"] == "some statement"
        assert len(result["falsifiers"]) == 2
        assert result["falsifiers"][0]["id"] == "F1"

    def test_missing_falsifiers_returns_empty_list(self, git_repo):
        """If falsifiers.jsonl does not exist at the ref, falsifiers == [] (no crash)."""
        repo = git_repo
        # Only events, no falsifiers
        events_content = '{"id": "evt1", "targets": []}\n'
        _write_file(repo, "ledger/events.jsonl", events_content)
        _git_add_commit(repo, "events only", "ledger/events.jsonl")
        result = read_ledger_at(repo)
        assert result["falsifiers"] == []

    def test_older_ref_returns_older_content(self, git_repo):
        """Reading at an older commit returns the content as it was at that commit."""
        repo = git_repo
        # First commit: one event
        _write_file(repo, "ledger/events.jsonl", '{"id": "old", "targets": []}\n')
        _git_add_commit(repo, "first", "ledger/events.jsonl")
        old_sha = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        # Second commit: overwrite events.jsonl
        _write_file(repo, "ledger/events.jsonl", '{"id": "new", "targets": []}\n')
        _git_add_commit(repo, "second", "ledger/events.jsonl")

        # Read at old commit
        old_result = read_ledger_at(repo, ref=old_sha)
        assert len(old_result["events"]) == 1
        assert old_result["events"][0]["id"] == "old"

        # Read at HEAD gets the new one
        head_result = read_ledger_at(repo)
        assert head_result["events"][0]["id"] == "new"

    def test_leading_dash_ref_raises_git_error(self, git_repo):
        """A ref starting with '-' raises GitError."""
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ref="--bad")

    def test_leading_dash_ledger_dir_raises_git_error(self, git_repo):
        """A ledger_dir starting with '-' raises GitError."""
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ledger_dir="-evil")


# --- Tests for projection_key ---

class TestProjectionKey:
    def test_stable_at_same_ref(self, git_repo):
        """Calling twice returns the same key."""
        repo = git_repo
        # Make sure there is some ledger content
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1", "targets": []}\n')
        _git_add_commit(repo, "initial", "ledger/events.jsonl")
        key1 = projection_key(repo)
        key2 = projection_key(repo)
        assert key1 == key2

    def test_changes_after_edit(self, git_repo):
        """After committing a change to a claim, the key changes."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1", "targets": []}\n')
        _write_file(repo, "ledger/claims/C1.json", json.dumps({"id": "C1", "title": "t1", "statement": "s1"}))
        _git_add_commit(repo, "first", "ledger/events.jsonl", "ledger/claims/C1.json")
        key_before = projection_key(repo)

        # Edit claim
        _write_file(repo, "ledger/claims/C1.json", json.dumps({"id": "C1", "title": "t1", "statement": "s2"}))
        _git_add_commit(repo, "edit claim", "ledger/claims/C1.json")
        key_after = projection_key(repo)
        assert key_before != key_after

    def test_equal_for_identical_ledger_tree_in_different_commits(self, git_repo):
        """Two commits with byte-identical ledger/ trees have equal keys."""
        repo = git_repo
        # Commit 1: some ledger content
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1", "targets": []}\n')
        _git_add_commit(repo, "first", "ledger/events.jsonl")
        key_commit1 = projection_key(repo)

        # Create an orphan commit with identical ledger content but different parent
        # We'll do a second commit that only changes a non-ledger file
        _write_file(repo, "notes.txt", "irrelevant")
        _git_add_commit(repo, "second", "notes.txt")
        key_commit2 = projection_key(repo)  # ledger unchanged
        assert key_commit1 == key_commit2

    def test_deterministic_prefix(self, git_repo):
        """The key string starts with 'proj:sha256:'."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1", "targets": []}\n')
        _git_add_commit(repo, "init", "ledger/events.jsonl")
        key = projection_key(repo)
        assert key.startswith("proj:sha256:")

    def test_leading_dash_raises(self, git_repo):
        with pytest.raises(GitError):
            projection_key(git_repo, ref="-bad")
        with pytest.raises(GitError):
            projection_key(git_repo, ledger_dir="-bad")


# --- Tests for diff_ledger ---

class TestDiffLedger:
    def test_adds_claims_and_events(self, git_repo):
        """A commit that adds a new claim and appends an event reports them."""
        repo = git_repo
        # Base commit: empty ledger (just the initial tracked file)
        # Actually the initial commit has ledger/events.jsonl empty? We'll make a base with one event.
        _write_file(repo, "ledger/events.jsonl", '{"id": "old", "targets": []}\n')
        _git_add_commit(repo, "base", "ledger/events.jsonl")
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        # Head commit: add a new claim and append an event
        _write_file(repo, "ledger/claims/Cy.json", json.dumps({"id": "Cy", "title": "new", "statement": "stmt"}))
        # Append
        with open(os.path.join(repo, "ledger/events.jsonl"), "a") as f:
            f.write('{"id": "new_event", "targets": []}\n')
        _git_add_commit(repo, "add", "ledger/events.jsonl", "ledger/claims/Cy.json")
        head = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        diff = diff_ledger(repo, base, head)
        assert "ledger/claims/Cy.json" in diff["added"]
        assert diff["claims_changed"] == ["Cy"]
        assert diff["events_changed"] is True
        assert diff["falsifiers_changed"] is False

    def test_deletes_claim(self, git_repo):
        """A commit that deletes a claim file reports it in deleted and claims_changed."""
        repo = git_repo
        # Base: create a claim
        _write_file(repo, "ledger/claims/Cz.json", json.dumps({"id": "Cz", "title": "del", "statement": "s"}))
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1", "targets": []}\n')
        _git_add_commit(repo, "base", "ledger/events.jsonl", "ledger/claims/Cz.json")
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        # Head: delete the claim file (git rm already stages the deletion — do NOT re-add it).
        _git_run(repo, "rm", "ledger/claims/Cz.json").check_returncode()
        _git_run(repo, "commit", "-m", "delete").check_returncode()
        head = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        diff = diff_ledger(repo, base, head)
        assert "ledger/claims/Cz.json" in diff["deleted"]
        assert "Cz" in diff["claims_changed"]

    def test_unrelated_change_returns_empty(self, git_repo):
        """A commit that changes nothing in ledger/ returns empty diffs."""
        repo = git_repo
        # Ensure a base with some ledger content
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1", "targets": []}\n')
        _git_add_commit(repo, "base", "ledger/events.jsonl")
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        # Unrelated commit: change a file outside ledger/
        _write_file(repo, "readme.md", "hello")
        _git_add_commit(repo, "unrelated", "readme.md")
        head = _git_run(repo, "rev-parse", "HEAD").stdout.strip()

        diff = diff_ledger(repo, base, head)
        assert diff["added"] == []
        assert diff["modified"] == []
        assert diff["deleted"] == []
        assert diff["claims_changed"] == []
        assert diff["events_changed"] is False
        assert diff["falsifiers_changed"] is False

    def test_leading_dash_base_raises(self, git_repo):
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="-bad", head="HEAD")

    def test_leading_dash_head_raises(self, git_repo):
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="HEAD", head="-bad")

    def test_leading_dash_ledger_dir_raises(self, git_repo):
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="HEAD", head="HEAD", ledger_dir="-bad")


# --- audit-round-1 convergence ---

class TestAuditR1Convergence:
    def test_modified_claim_reported(self, git_repo):
        """A MODIFIED claim file -> 'M' path in modified + claims_changed (audit-r1 S5)."""
        repo = git_repo
        _write_file(repo, "ledger/claims/Cm.json", json.dumps({"id": "Cm", "statement": "v1"}))
        _git_add_commit(repo, "base", "ledger/claims/Cm.json")
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        _write_file(repo, "ledger/claims/Cm.json", json.dumps({"id": "Cm", "statement": "v2"}))
        _git_add_commit(repo, "modify", "ledger/claims/Cm.json")
        head = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        d = diff_ledger(repo, base, head)
        assert "ledger/claims/Cm.json" in d["modified"]
        assert d["claims_changed"] == ["Cm"]

    def test_renamed_claim_reported(self, git_repo):
        """A RENAMED claim -> old in deleted + new in added; both stems in claims_changed.
        Uses -z token parsing (audit-r1 S2/S5)."""
        repo = git_repo
        _write_file(repo, "ledger/claims/Cold.json", json.dumps({"id": "Cold", "statement": "s"}))
        _git_add_commit(repo, "base", "ledger/claims/Cold.json")
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        _git_run(repo, "mv", "ledger/claims/Cold.json", "ledger/claims/Cnew.json").check_returncode()
        _git_run(repo, "commit", "-m", "rename").check_returncode()
        head = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        # force rename detection so we exercise the R<score> path of the -z parser.
        d = diff_ledger(repo, base, head)
        assert "ledger/claims/Cnew.json" in d["added"]
        assert "ledger/claims/Cold.json" in d["deleted"]
        assert set(d["claims_changed"]) == {"Cold", "Cnew"}

    def test_empty_events_and_falsifiers_files(self, git_repo):
        """An events/falsifiers file that EXISTS but is empty -> [] (not a crash) (audit-r1 S5)."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", "")
        _write_file(repo, "ledger/falsifiers.jsonl", "")
        _git_add_commit(repo, "empty truth files", "ledger/events.jsonl", "ledger/falsifiers.jsonl")
        snap = read_ledger_at(repo)
        assert snap["events"] == []
        assert snap["falsifiers"] == []

    def test_missing_claims_dir_yields_empty_dict(self, git_repo):
        """No ledger/claims/ at the ref -> claims == {} (audit-r1 S5)."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1"}\n')
        _git_add_commit(repo, "events only, no claims dir", "ledger/events.jsonl")
        snap = read_ledger_at(repo)
        assert snap["claims"] == {}

    def test_projection_key_empty_tree_is_stable(self, git_repo):
        """An empty/absent ledger_dir -> a stable key, not a crash (audit-r1 S5)."""
        repo = git_repo
        k1 = projection_key(repo, ledger_dir="nonexistent")
        k2 = projection_key(repo, ledger_dir="nonexistent")
        assert k1 == k2 and k1.startswith("proj:sha256:")

    def test_projection_key_nested_ledger_dir(self, git_repo):
        """A nested ledger_dir composes correctly and is stable/changes-on-edit (audit-r1 S5)."""
        repo = git_repo
        _write_file(repo, "sub/led/events.jsonl", '{"id": "e1"}\n')
        _git_add_commit(repo, "nested", "sub/led/events.jsonl")
        k1 = projection_key(repo, ledger_dir="sub/led")
        assert k1 == projection_key(repo, ledger_dir="sub/led")
        _write_file(repo, "sub/led/events.jsonl", '{"id": "e1"}\n{"id": "e2"}\n')
        _git_add_commit(repo, "nested edit", "sub/led/events.jsonl")
        assert projection_key(repo, ledger_dir="sub/led") != k1

    def test_projection_key_path_with_spaces_deterministic(self, git_repo):
        """A claim filename with SPACES must not corrupt the key (the -z fix, audit-r1 S1/S2)."""
        repo = git_repo
        _write_file(repo, "ledger/claims/C spaced.json", json.dumps({"id": "C spaced"}))
        _git_add_commit(repo, "spaced", "ledger/claims/C spaced.json")
        k1 = projection_key(repo)
        assert k1 == projection_key(repo)  # deterministic despite the space
        # the spaced claim is read back with its true (unquoted) path/id.
        snap = read_ledger_at(repo)
        assert "C spaced" in snap["claims"]
        # and diff sees the unquoted path.
        base = _git_run(repo, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        d = diff_ledger(repo, base, "HEAD")
        assert "ledger/claims/C spaced.json" in d["added"]
        assert "C spaced" in d["claims_changed"]

    def test_diff_ledger_keys_on_filename_stem(self, git_repo):
        """CONTRACT: claims_changed is the filename STEM (the ledger's one-claim-per-file convention
        makes stem == claim id). Pins the documented behavior (audit-r1 S2/S5 — rejected as a
        contract disagreement)."""
        repo = git_repo
        # filename stem 'Cfile' intentionally != internal id 'Cinternal'.
        _write_file(repo, "ledger/claims/Cfile.json", json.dumps({"id": "Cinternal"}))
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        _git_add_commit(repo, "add", "ledger/claims/Cfile.json")
        d = diff_ledger(repo, base, "HEAD")
        assert d["claims_changed"] == ["Cfile"]  # the STEM, per contract

    def test_double_dot_guard(self, git_repo):
        """'..' (path-traversal / ref-range) fails closed on every positional (audit-r1 S3)."""
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ref="HEAD..main")
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ledger_dir="../escape")
        with pytest.raises(GitError):
            projection_key(git_repo, ledger_dir="../escape")
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="a..b", head="HEAD")


# --- audit-round-2 convergence ---

class TestAuditR2Convergence:
    def test_projection_key_ledger_dir_trailing_slash_normalized(self, git_repo):
        """'ledger' and 'ledger/' yield the SAME key (normalization keeps it content-addressed,
        audit-r2 S2.1)."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1"}\n')
        _git_add_commit(repo, "c", "ledger/events.jsonl")
        assert projection_key(repo, ledger_dir="ledger") == projection_key(repo, ledger_dir="ledger/")

    def test_empty_ledger_dir_and_ref_rejected(self, git_repo):
        """An empty/blank ledger_dir (would scan the whole repo) or ref fails closed (audit-r2 S2.2)."""
        with pytest.raises(GitError):
            projection_key(git_repo, ledger_dir="")
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ledger_dir="   ")
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ref="")

    def test_claims_changed_is_sorted(self, git_repo):
        """Multiple changed claims -> claims_changed is SORTED, not insertion/ls order (audit-r2 S5)."""
        repo = git_repo
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        # add in deliberately non-sorted order
        for cid in ("Cz", "Ca", "Cm"):
            _write_file(repo, f"ledger/claims/{cid}.json", json.dumps({"id": cid}))
        _git_add_commit(repo, "add three claims",
                        "ledger/claims/Cz.json", "ledger/claims/Ca.json", "ledger/claims/Cm.json")
        d = diff_ledger(repo, base, "HEAD")
        assert d["claims_changed"] == ["Ca", "Cm", "Cz"]  # sorted


# --- audit-round-3 convergence ---

class TestAuditR3Convergence:
    def test_projection_key_bad_ref_raises(self, git_repo):
        """A non-existent ref RAISES (not a silent empty-tree key), matching read_ledger_at
        (audit-r3 S1/S2)."""
        with pytest.raises(GitError):
            projection_key(git_repo, ref="no_such_ref_xyz")

    def test_read_ledger_at_reads_blob_not_dirty_worktree(self, git_repo):
        """read_ledger_at returns the COMMITTED blob even when the worktree has diverged
        (proves git-blob read, not a plain file read) (audit-r3 S5)."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", '{"id": "committed"}\n')
        _git_add_commit(repo, "commit truth", "ledger/events.jsonl")
        # dirty the worktree WITHOUT committing
        _write_file(repo, "ledger/events.jsonl", '{"id": "DIRTY_uncommitted"}\n')
        snap = read_ledger_at(repo, "HEAD")
        assert [e["id"] for e in snap["events"]] == ["committed"]

    def test_diff_falsifiers_changed_true_on_add(self, git_repo):
        """falsifiers_changed True path (add) — not just the False path (audit-r3 S5)."""
        repo = git_repo
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        _write_file(repo, "ledger/falsifiers.jsonl", '{"id": "F1"}\n')
        _git_add_commit(repo, "add falsifiers", "ledger/falsifiers.jsonl")
        d = diff_ledger(repo, base, "HEAD")
        assert d["falsifiers_changed"] is True
        assert "ledger/falsifiers.jsonl" in d["added"]

    def test_diff_events_changed_on_delete(self, git_repo):
        """events_changed True on DELETE (the fixture commits events.jsonl at init) (audit-r3 S5)."""
        repo = git_repo
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()  # fixture has events.jsonl
        _git_run(repo, "rm", "ledger/events.jsonl").check_returncode()
        _git_run(repo, "commit", "-m", "rm events").check_returncode()
        d = diff_ledger(repo, base, "HEAD")
        assert d["events_changed"] is True
        assert "ledger/events.jsonl" in d["deleted"]

    def test_diff_empty_base_head_rejected(self, git_repo):
        """Empty/blank base or head fails closed in diff_ledger (audit-r3 S5)."""
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="", head="HEAD")
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="HEAD", head="   ")

    def test_trailing_slash_normalized_in_read_and_diff(self, git_repo):
        """read_ledger_at and diff_ledger accept "ledger/" identically to "ledger" (shared _norm_dir,
        audit-r3 S5)."""
        repo = git_repo
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        _write_file(repo, "ledger/claims/Cx.json", json.dumps({"id": "Cx"}))
        _git_add_commit(repo, "add", "ledger/claims/Cx.json")
        assert read_ledger_at(repo, "HEAD", ledger_dir="ledger") == \
            read_ledger_at(repo, "HEAD", ledger_dir="ledger/")
        assert diff_ledger(repo, base, "HEAD", ledger_dir="ledger") == \
            diff_ledger(repo, base, "HEAD", ledger_dir="ledger/")


# --- audit-round-4 convergence ---

class TestAuditR4Convergence:
    def test_claim_null_or_nonstring_id_falls_back_to_stem(self, git_repo):
        """A claim with "id": null or a non-string id falls back to the filename STEM — never a
        None/int dict key (audit-r4 S0)."""
        repo = git_repo
        _write_file(repo, "ledger/claims/Cnull.json", json.dumps({"id": None, "statement": "x"}))
        _write_file(repo, "ledger/claims/Cint.json", json.dumps({"id": 7, "statement": "y"}))
        _write_file(repo, "ledger/claims/Cmissing.json", json.dumps({"statement": "z"}))
        _git_add_commit(repo, "odd ids",
                        "ledger/claims/Cnull.json", "ledger/claims/Cint.json", "ledger/claims/Cmissing.json")
        claims = read_ledger_at(repo)["claims"]
        assert None not in claims and 7 not in claims
        assert "Cnull" in claims and "Cint" in claims and "Cmissing" in claims
        assert all(isinstance(k, str) for k in claims)

    def test_norm_dir_strips_leading_dot_slash(self, git_repo):
        """"./ledger" normalizes to "ledger" -> same projection key (audit-r4 S1)."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl", '{"id": "e1"}\n')
        _git_add_commit(repo, "c", "ledger/events.jsonl")
        assert projection_key(repo, ledger_dir="./ledger") == projection_key(repo, ledger_dir="ledger")

    def test_read_ledger_at_bad_ref_raises(self, git_repo):
        """A non-existent ref raises GitError in read_ledger_at (audit-r4 S5)."""
        with pytest.raises(GitError):
            read_ledger_at(git_repo, ref="no_such_ref_xyz")

    def test_diff_ledger_bad_ref_raises(self, git_repo):
        """A non-existent base/head raises GitError in diff_ledger (audit-r4 S5)."""
        with pytest.raises(GitError):
            diff_ledger(git_repo, base="no_such_ref_xyz", head="HEAD")

    def test_read_ledger_at_filters_lines_without_id(self, git_repo):
        """events/falsifiers lines WITHOUT an "id" are dropped (the filtering spec, audit-r4 S5)."""
        repo = git_repo
        _write_file(repo, "ledger/events.jsonl",
                    '{"id": "keep"}\n{"no_id": 1}\n{"id": "keep2"}\n')
        _git_add_commit(repo, "mixed", "ledger/events.jsonl")
        ids = [e["id"] for e in read_ledger_at(repo)["events"]]
        assert ids == ["keep", "keep2"]

    def test_diff_falsifiers_changed_on_modify_and_delete(self, git_repo):
        """falsifiers_changed True on MODIFY and DELETE too (symmetry with events, audit-r4 S5)."""
        repo = git_repo
        _write_file(repo, "ledger/falsifiers.jsonl", '{"id": "F1"}\n')
        _git_add_commit(repo, "base falsifiers", "ledger/falsifiers.jsonl")
        base = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        # modify
        _write_file(repo, "ledger/falsifiers.jsonl", '{"id": "F1"}\n{"id": "F2"}\n')
        _git_add_commit(repo, "modify falsifiers", "ledger/falsifiers.jsonl")
        d_mod = diff_ledger(repo, base, "HEAD")
        assert d_mod["falsifiers_changed"] is True
        assert "ledger/falsifiers.jsonl" in d_mod["modified"]
        # delete
        mid = _git_run(repo, "rev-parse", "HEAD").stdout.strip()
        _git_run(repo, "rm", "ledger/falsifiers.jsonl").check_returncode()
        _git_run(repo, "commit", "-m", "rm falsifiers").check_returncode()
        d_del = diff_ledger(repo, mid, "HEAD")
        assert d_del["falsifiers_changed"] is True
        assert "ledger/falsifiers.jsonl" in d_del["deleted"]
