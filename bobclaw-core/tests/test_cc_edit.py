"""
BoBClaw Core — C4 approved-edit path tests (mock; no spawn, no network).

Covers:
  * diff parser: extracts file_path + body from proposed_<n>.diff (1 + many
    files); ignores prose.
  * permissions: requires_approval("cc_edit") is True; route_approval -> "human".
  * a planner-cc-edit turn that produced a scratch diff emits a cc_edit
    approval_request with the right details (incl. scope), approval_required.
  * apply against a tmp git repo: approve → file matches; reject → no change;
    malformed diff → error, tree untouched; gated by CC_EDIT_APPLY_ENABLED.
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap

import pytest

from core.nodes import cc_edit
from core.nodes.cc_edit import (
    CCApplyError,
    apply_cc_edit,
    capture_cc_edit,
    parse_inline_diffs,
    parse_scratch_diffs,
    parse_unified_diff,
    route_approval,
    split_unified_diff_by_file,
)
from core.permissions import requires_approval


# ── fixtures ─────────────────────────────────────────────────────────────────

_DIFF_HELLO = textwrap.dedent(
    """\
    diff --git a/hello.txt b/hello.txt
    index 0000001..0000002 100644
    --- a/hello.txt
    +++ b/hello.txt
    @@ -1 +1 @@
    -hello
    +hello world
    """
)

_DIFF_OTHER = textwrap.dedent(
    """\
    --- a/other.txt
    +++ b/other.txt
    @@ -1 +1 @@
    -foo
    +bar
    """
)


# ── diff parser ──────────────────────────────────────────────────────────────

def test_parse_unified_diff_extracts_file_and_body():
    parsed = parse_unified_diff(_DIFF_HELLO)
    assert parsed is not None
    assert parsed["file_path"] == "hello.txt"
    assert "+hello world" in parsed["unified_diff"]


def test_parse_unified_diff_ignores_prose():
    assert parse_unified_diff("Here is my plan. I will edit hello.txt.") is None
    assert parse_unified_diff("") is None
    # A hunk marker with no file header is not self-applicable.
    assert parse_unified_diff("@@ -1 +1 @@\n-a\n+b\n") is None


def test_parse_scratch_diffs_single(tmp_path):
    (tmp_path / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    (tmp_path / "notes.md").write_text("just prose, ignore me", encoding="utf-8")
    diffs = parse_scratch_diffs(str(tmp_path))
    assert len(diffs) == 1
    assert diffs[0]["file_path"] == "hello.txt"


def test_parse_scratch_diffs_multiple_ordered(tmp_path):
    # Write out of order; expect numeric order proposed_1 then proposed_2.
    (tmp_path / "proposed_2.diff").write_text(_DIFF_OTHER, encoding="utf-8")
    (tmp_path / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    diffs = parse_scratch_diffs(str(tmp_path))
    assert [d["file_path"] for d in diffs] == ["hello.txt", "other.txt"]


def test_parse_scratch_diffs_missing_dir():
    assert parse_scratch_diffs("/no/such/dir/xyz") == []


def test_parse_inline_diffs_fenced_block():
    reply = "Sure, here is the change:\n```diff\n" + _DIFF_HELLO + "```\nDone."
    diffs = parse_inline_diffs(reply)
    assert len(diffs) == 1
    assert diffs[0]["file_path"] == "hello.txt"


def test_capture_prefers_scratch_over_inline(tmp_path):
    (tmp_path / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    reply = "```diff\n" + _DIFF_OTHER + "```"
    diffs = capture_cc_edit(str(tmp_path), reply)
    # Scratch wins.
    assert [d["file_path"] for d in diffs] == ["hello.txt"]


def test_capture_falls_back_to_inline(tmp_path):
    reply = "```diff\n" + _DIFF_OTHER + "```"
    diffs = capture_cc_edit(str(tmp_path), reply)
    assert [d["file_path"] for d in diffs] == ["other.txt"]


def test_capture_none_found(tmp_path):
    assert capture_cc_edit(str(tmp_path), "no diff here, just chatting") == []


# ── permissions + routing seam ───────────────────────────────────────────────

def test_cc_edit_requires_approval():
    assert requires_approval("cc_edit") is True


@pytest.mark.asyncio
async def test_route_approval_cc_edit_is_human():
    assert await route_approval("cc_edit", {"scope": {}}) == "human"


# ── apply against a tmp git repo ─────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """A real (tmp) git repo with a tracked hello.txt = 'hello\\n'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    run = lambda *a: subprocess.run(
        ["git", *a], cwd=repo, check=True, capture_output=True, text=True, env=env
    )
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Test")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return repo


def test_apply_approve_changes_file(git_repo):
    apply_cc_edit(_DIFF_HELLO, str(git_repo))
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello world\n"


def test_apply_malformed_diff_errors_tree_untouched(git_repo):
    bad = textwrap.dedent(
        """\
        --- a/hello.txt
        +++ b/hello.txt
        @@ -1 +1 @@
        -this line does not match the file
        +replacement
        """
    )
    with pytest.raises(CCApplyError):
        apply_cc_edit(bad, str(git_repo))
    # Tree untouched — check ran first, real apply never reached.
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_apply_empty_diff_errors(git_repo):
    with pytest.raises(CCApplyError):
        apply_cc_edit("   ", str(git_repo))
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_apply_does_not_commit(git_repo):
    apply_cc_edit(_DIFF_HELLO, str(git_repo))
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo, capture_output=True, text=True,
    ).stdout
    # Change is in the working tree, not committed.
    assert "hello.txt" in status


def test_apply_non_ascii_diff(git_repo):
    """Regression (C4 live E2E 2026-06-15): a diff with non-ASCII content (em-dash)
    must apply. Before forcing UTF-8 on the git-apply subprocess, the stdin writer
    crashed under the Windows cp1252 default and `git apply -` hung to the timeout."""
    diff = textwrap.dedent(
        """\
        diff --git a/hello.txt b/hello.txt
        index 0000001..0000002 100644
        --- a/hello.txt
        +++ b/hello.txt
        @@ -1 +1 @@
        -hello
        +hello — world
        """
    )
    apply_cc_edit(diff, str(git_repo))
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello — world\n"


# ── execute_node integration: capture + resolve ──────────────────────────────

@pytest.fixture
def patched_cc_client(monkeypatch, tmp_path):
    """Stub the claude_code spawn so execute_node's CC block returns a known
    response, and point the client scratch dir at *tmp_path*."""
    from core.backends import claude_code as cc_mod

    scratch = tmp_path / "scratch"
    scratch.mkdir()

    async def fake_chat(self, *, prompt, resume_session_id=None, posture=None):
        self.last_session_id = "sess-xyz"
        return {"text": "Proposed the edit (see scratch).", "session_id": "sess-xyz",
                "is_error": False, "api_error_status": None, "raw": {}}

    monkeypatch.setattr(cc_mod.ClaudeCodeClient, "chat", fake_chat)
    monkeypatch.setattr(
        cc_mod.ClaudeCodeClient, "_scratch_dir", lambda self: str(scratch)
    )
    # No memory L0 writes / session sidecar in the test.
    import core.nodes.execute as ex
    import core.cc_sessions as sess
    from core.config import config

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ex, "_append_agent_turn_event", _noop)
    monkeypatch.setattr(sess, "_record_cc_session", _noop)
    monkeypatch.setattr(sess, "_lookup_cc_session", lambda *a, **k: _noop())
    # Deterministic, OFFLINE Gate: with no critic backend, an ambiguous ("gate")
    # cc_edit decision resolves on the deterministic floor (→ human) instead of a
    # live MiniMax `run_critic` call. (GATE_CRITIC_BACKEND defaults to "minimax",
    # which made test_planner_cc_edit_turn_emits_cc_edit_approval hit the network and
    # flake. Auto-clear cases — auto_actions+may_touch → "auto" — never reach the
    # critic, so they are unaffected.)
    monkeypatch.setattr(config, "GATE_CRITIC_BACKEND", "")
    return scratch


@pytest.mark.asyncio
async def test_planner_cc_edit_turn_emits_cc_edit_approval(patched_cc_client):
    from core.nodes.execute import execute_node

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")

    state = {
        "task": "tweak hello",
        "backend": "claude_code",
        "messages": [],
        "conversation_id": "conv-1",
        "cc_posture": {"mode": "scratch_write", "scope": {"branch": "feat/x", "may_touch": ["hello.txt"]}},
    }
    out = await execute_node(state)

    assert out["approval_required"] is True
    assert out["approval_action_type"] == "cc_edit"
    details = out["approval_details"]
    assert details["file_path"] == "hello.txt"
    assert "+hello world" in details["diff"]
    assert details["conversation_id"] == "conv-1"
    assert details["scope"] == {"branch": "feat/x", "may_touch": ["hello.txt"]}
    assert out["cc_pending_edit"]["diff"]


@pytest.mark.asyncio
async def test_cc_edit_captured_even_when_claude_code_throttles(patched_cc_client, monkeypatch):
    """Hardening: a scratch-write planning turn whose claude_code call THROTTLES must
    still surface its proposed edit (the diff is a file on disk) — not silently lose it
    to the escalation backend. The capture runs on the throttle path too."""
    from core.backends.claude_code import ClaudeCodeThrottled
    from core.backends import claude_code as cc_mod
    from core.nodes.execute import execute_node

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")

    async def _throttle(self, *, prompt, resume_session_id=None, posture=None):
        raise ClaudeCodeThrottled("claude CLI throttled [overload]: Overloaded (529)")

    monkeypatch.setattr(cc_mod.ClaudeCodeClient, "chat", _throttle)

    state = {
        "task": "tweak hello",
        "backend": "claude_code",
        "escalation_backend": "claude_api",   # would be used if the edit were lost
        "messages": [],
        "conversation_id": "conv-throttle",
        "cc_posture": {"mode": "scratch_write", "scope": {"may_touch": ["hello.txt"]}},
    }
    out = await execute_node(state)

    # Captured + parked despite the throttle — NOT escalated to claude_api.
    assert out["approval_required"] is True
    assert out["approval_action_type"] == "cc_edit"
    assert out["approval_details"]["file_path"] == "hello.txt"
    assert out["cc_pending_edit"]["diff"]


# ── Neck Beard P3: token scope precedence + gateable cc_edit auto-clear ───────

@pytest.mark.asyncio
async def test_cc_edit_token_scope_overrides_posture(patched_cc_client):
    """P3: the agent TOKEN scope (AgentState.scope) takes precedence over the face
    posture scope. A token scope that forbids the edited path forces HUMAN even when
    the posture scope alone would have auto-cleared it."""
    from core.nodes.execute import execute_node

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")

    token_scope = {"may_not_touch": ["hello.txt"]}           # forces human on this path
    state = {
        "task": "tweak hello", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-prec",
        # posture scope WOULD auto-clear (cc_edit + may_touch) — must be overridden
        "cc_posture": {"mode": "scratch_write",
                       "scope": {"auto_actions": ["cc_edit"], "may_touch": ["hello.txt"]}},
        "scope": token_scope,
    }
    out = await execute_node(state)
    assert out["approval_required"] is True
    assert out["approval_action_type"] == "cc_edit"
    assert out["approval_details"]["scope"] == token_scope   # the Gate saw the TOKEN scope


@pytest.mark.asyncio
async def test_cc_edit_gate_auto_applies_in_scope(patched_cc_client, monkeypatch, git_repo):
    """P3 dogfood core: an in-scope cc_edit (cc_edit ∈ auto_actions + path ∈ may_touch)
    auto-clears the Gate and is applied core-direct (CC_EDIT_APPLY_ENABLED on). No human
    interrupt; the working tree changes."""
    from core.config import config
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    state = {
        "task": "tweak hello", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-auto", "user_id": "admin",
        "cc_posture": {"mode": "scratch_write"},   # posture has NO scope
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out.get("approval_required") is False
    assert "approval_action_type" not in out
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello world\n"
    assert any("auto-applied by the Gate" in m["content"] for m in out.get("messages", []))


@pytest.mark.asyncio
async def test_cc_edit_gate_auto_clear_apply_disabled_does_not_write(
    patched_cc_client, monkeypatch, git_repo
):
    """Auto-clear stays double-gated by CC_EDIT_APPLY_ENABLED: off → captured, NOT
    applied (tree untouched), even though the Gate cleared it."""
    from core.config import config
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", False)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    state = {
        "task": "tweak hello", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-auto-off", "user_id": "admin",
        "cc_posture": {"mode": "scratch_write"},
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out.get("approval_required") is False
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"   # untouched
    assert any("apply is disabled" in m["content"] for m in out.get("messages", []))


# A pure-deletion diff (+++ /dev/null, no `diff --git`) → the parser yields file_path=None
# (path is NOT scope-checkable) yet the hunk IS in the applied combined diff.
_DIFF_DELETION = textwrap.dedent(
    """\
    --- a/secret.txt
    +++ /dev/null
    @@ -1 +0,0 @@
    -topsecret
    """
)


class _FakePool:
    """Captures asyncpg execute() calls so the gate-audit write can be asserted."""

    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


@pytest.mark.asyncio
async def test_cc_edit_gate_auto_apply_writes_audit_row(patched_cc_client, monkeypatch, git_repo):
    """Regression (review finding #3): the approved_by='gate' audit row IS written for an
    auto-cleared cc_edit — i.e. json.dumps in _audit_cc_edit_gate does not NameError (json
    was un-imported). Prior tests missed it: get_pool() raised first (no pool), so the
    json.dumps line was never reached."""
    import core.db as cdb
    from core.config import config
    from core.nodes.execute import execute_node

    fake = _FakePool()
    monkeypatch.setattr(cdb, "get_pool", lambda: fake)
    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    state = {
        "task": "tweak hello", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-audit", "user_id": "admin",
        "cc_posture": {"mode": "scratch_write"},
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out.get("approval_required") is False
    assert len(fake.calls) == 1                              # exactly one audit row
    query, args = fake.calls[0]
    assert "INSERT INTO approvals" in query
    # (conv_uuid, user_id, action_type, details_json, status, approved_by)
    assert args[1] == "admin" and args[2] == "cc_edit"
    assert args[4] == "approved" and args[5] == "gate"
    assert json.loads(args[3])["file_paths"] == ["hello.txt"]   # json.dumps worked


@pytest.mark.asyncio
async def test_cc_edit_unparseable_path_diff_never_auto_applies(
    patched_cc_client, monkeypatch, git_repo
):
    """Regression (review finding #2): a diff whose path the parser can't identify
    (+++ /dev/null deletion) is excluded from the Gate path check — it must NOT ride an
    auto-clear even when the scope would auto-approve cc_edit. Fail closed to human."""
    from core.config import config
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))
    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_DELETION, encoding="utf-8")
    state = {
        "task": "rm secret", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-del", "user_id": "admin",
        "cc_posture": {"mode": "scratch_write"},
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["**"]},
    }
    out = await execute_node(state)
    assert out["approval_required"] is True               # parked, NOT auto-applied
    assert out["approval_action_type"] == "cc_edit"


@pytest.mark.asyncio
async def test_planner_cc_edit_no_diff_is_normal_turn(patched_cc_client):
    from core.nodes.execute import execute_node

    # No proposed_*.diff written → normal planner turn, no approval.
    state = {
        "task": "just plan",
        "backend": "claude_code",
        "messages": [],
        "conversation_id": "conv-2",
        "cc_posture": {"mode": "scratch_write"},
    }
    out = await execute_node(state)
    assert out.get("approval_required") is False
    assert "approval_action_type" not in out


@pytest.mark.asyncio
async def test_resolve_cc_edit_approve_applies(monkeypatch, git_repo):
    from core.config import config
    import core.nodes.execute as ex
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(ex, "_append_agent_turn_event", _noop)

    state = {
        "task": "x",
        "backend": "claude_code",
        "messages": [],
        "approval_response": "approve",
        "cc_pending_edit": {"diff": _DIFF_HELLO, "file_paths": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out["approval_required"] is False
    assert out["cc_pending_edit"] is None
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello world\n"


@pytest.mark.asyncio
async def test_resolve_cc_edit_reject_no_change(monkeypatch, git_repo):
    from core.config import config
    import core.nodes.execute as ex
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(ex, "_append_agent_turn_event", _noop)

    state = {
        "task": "x",
        "backend": "claude_code",
        "messages": [],
        "approval_response": "reject",
        "cc_pending_edit": {"diff": _DIFF_HELLO, "file_paths": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out["approval_required"] is False
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_resolve_cc_edit_gated_off_does_not_apply(monkeypatch, git_repo):
    from core.config import config
    import core.nodes.execute as ex
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", False)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(ex, "_append_agent_turn_event", _noop)

    state = {
        "task": "x",
        "backend": "claude_code",
        "messages": [],
        "approval_response": "approve",
        "cc_pending_edit": {"diff": _DIFF_HELLO, "file_paths": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out["approval_required"] is False
    # Feature gate off → captured but not applied.
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_resolve_cc_edit_honours_edit_content(monkeypatch, git_repo):
    from core.config import config
    import core.nodes.execute as ex
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(ex, "_append_agent_turn_event", _noop)

    edited = textwrap.dedent(
        """\
        --- a/hello.txt
        +++ b/hello.txt
        @@ -1 +1 @@
        -hello
        +edited by user
        """
    )
    state = {
        "task": "x",
        "backend": "claude_code",
        "messages": [],
        "approval_response": "approve",
        "approval_edit_content": edited,
        "cc_pending_edit": {"diff": _DIFF_HELLO, "file_paths": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out["approval_required"] is False
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "edited by user\n"


# ── F1: multi-file split so the Gate sees EVERY path ─────────────────────────

# One proposed diff carrying TWO files. Before F1 this collapsed to a single entry
# whose file_path was only "hello.txt" (the first +++ b/), so a scope that allowed
# hello.txt auto-cleared and git apply wrote BOTH — including EVIL.txt.
_DIFF_TWO_FILES = textwrap.dedent(
    """\
    diff --git a/hello.txt b/hello.txt
    index 0000001..0000002 100644
    --- a/hello.txt
    +++ b/hello.txt
    @@ -1 +1 @@
    -hello
    +hello world
    diff --git a/EVIL.txt b/EVIL.txt
    new file mode 100644
    index 0000000..0000003
    --- /dev/null
    +++ b/EVIL.txt
    @@ -0,0 +1 @@
    +pwned
    """
)

# A plain (no `diff --git`) two-file diff.
_DIFF_TWO_FILES_PLAIN = textwrap.dedent(
    """\
    --- a/hello.txt
    +++ b/hello.txt
    @@ -1 +1 @@
    -hello
    +hello world
    --- a/other.txt
    +++ b/other.txt
    @@ -1 +1 @@
    -foo
    +bar
    """
)

# A single-file diff whose hunk BODY contains lines that look like file headers
# (` --- a/evil.txt` / ` +++ b/evil.txt` as context). A hand parser that splits on
# `--- `/`+++ ` would wrongly see a second file; hunk-line-counting must not.
_DIFF_HEADER_LOOKALIKE_BODY = textwrap.dedent(
    """\
    diff --git a/real.txt b/real.txt
    --- a/real.txt
    +++ b/real.txt
    @@ -1,3 +1,3 @@
    -alpha
    +beta
     --- a/evil.txt
     +++ b/evil.txt
    """
)


def test_split_unified_diff_by_file_git_style_multifile():
    secs = split_unified_diff_by_file(_DIFF_TWO_FILES)
    assert [s["file_path"] for s in secs] == ["hello.txt", "EVIL.txt"]
    # Each sub-diff is self-contained: contains its own file's hunk, not the other's.
    assert "+hello world" in secs[0]["unified_diff"] and "+pwned" not in secs[0]["unified_diff"]
    assert "+pwned" in secs[1]["unified_diff"] and "+hello world" not in secs[1]["unified_diff"]


def test_split_unified_diff_by_file_plain_multifile():
    secs = split_unified_diff_by_file(_DIFF_TWO_FILES_PLAIN)
    assert [s["file_path"] for s in secs] == ["hello.txt", "other.txt"]


def test_split_does_not_split_on_header_lookalike_hunk_body():
    # The ` --- a/evil.txt` / ` +++ b/evil.txt` context lines must NOT start a section.
    secs = split_unified_diff_by_file(_DIFF_HEADER_LOOKALIKE_BODY)
    assert [s["file_path"] for s in secs] == ["real.txt"]


# A first hunk whose header DECLARES 5 lines but provides only 1 (truncated / count
# inflation) immediately before a second file. A naive line-counter would absorb the
# `diff --git a/EVIL.txt` header into hunk 1, merging both files → Gate sees only in.txt.
_DIFF_MALFORMED_COUNT = textwrap.dedent(
    """\
    diff --git a/in.txt b/in.txt
    --- a/in.txt
    +++ b/in.txt
    @@ -1,5 +1,5 @@
    -hello
    +hi
    diff --git a/EVIL.txt b/EVIL.txt
    new file mode 100644
    --- /dev/null
    +++ b/EVIL.txt
    @@ -0,0 +1 @@
    +pwned
    """
)


def test_split_malformed_hunk_count_does_not_absorb_next_file():
    """Fleet-audit finding: an inflated hunk count must NOT swallow the next file's
    `diff --git` header — both paths must still surface so the Gate sees EVIL.txt."""
    assert [s["file_path"] for s in split_unified_diff_by_file(_DIFF_MALFORMED_COUNT)] == [
        "in.txt",
        "EVIL.txt",
    ]


def test_split_crlf_multifile():
    secs = split_unified_diff_by_file(_DIFF_TWO_FILES.replace("\n", "\r\n"))
    assert [s["file_path"] for s in secs] == ["hello.txt", "EVIL.txt"]
    # CRLF preserved in the reconstructed sub-diff (so git apply gets the original bytes).
    assert "\r\n" in secs[0]["unified_diff"]


def test_split_prose_and_bare_hunk_yield_nothing():
    assert split_unified_diff_by_file("just a plan, no diff") == []
    assert split_unified_diff_by_file("") == []
    assert split_unified_diff_by_file("@@ -1 +1 @@\n-a\n+b\n") == []  # hunk, no header


def test_capture_multifile_scratch_splits(tmp_path):
    (tmp_path / "proposed_1.diff").write_text(_DIFF_TWO_FILES, encoding="utf-8")
    diffs = capture_cc_edit(str(tmp_path), "")
    assert [d["file_path"] for d in diffs] == ["hello.txt", "EVIL.txt"]


@pytest.mark.asyncio
async def test_cc_edit_multifile_one_out_of_scope_parks(
    patched_cc_client, monkeypatch, git_repo
):
    """F1 regression: a single proposed diff with two files — one in `may_touch`, one NOT
    — must PARK (the out-of-scope file makes the whole edit human-gated). It must NOT
    auto-apply, so EVIL.txt is never written and hello.txt is left untouched."""
    from core.config import config
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_TWO_FILES, encoding="utf-8")
    state = {
        "task": "tweak hello", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-multi", "user_id": "admin",
        "cc_posture": {"mode": "scratch_write"},
        # cc_edit auto + hello.txt may_touch WOULD auto-clear hello.txt alone…
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["hello.txt"]},
    }
    out = await execute_node(state)

    assert out["approval_required"] is True               # parked, NOT auto-applied
    assert out["approval_action_type"] == "cc_edit"
    # The Gate saw BOTH paths (this is the fix — pre-F1 it saw only hello.txt).
    assert set(out["approval_details"]["file_paths"]) == {"hello.txt", "EVIL.txt"}
    # Nothing written: EVIL.txt never created, hello.txt unchanged.
    assert not (git_repo / "EVIL.txt").exists()
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


# ── F2: filesystem containment + Gate-set belt on the apply path ─────────────

def test_apply_rejects_path_traversal(git_repo, tmp_path):
    """A diff whose target resolves OUTSIDE the project dir (`../`) is refused before any
    write — tree (and the parent dir) untouched."""
    traversal = textwrap.dedent(
        """\
        diff --git a/../escape.txt b/../escape.txt
        new file mode 100644
        --- /dev/null
        +++ b/../escape.txt
        @@ -0,0 +1 @@
        +escaped
        """
    )
    with pytest.raises(CCApplyError):
        apply_cc_edit(traversal, str(git_repo))
    # The escape target (one level up = tmp_path) was never created.
    assert not (git_repo.parent / "escape.txt").exists()


def test_apply_gated_paths_rejects_unvetted_file(git_repo):
    """F1 belt-and-braces: even if a multi-file body reached apply, a path git would
    touch that the Gate never vetted (`gated_paths`) is refused — EVIL.txt not written,
    hello.txt untouched (whole-or-nothing)."""
    with pytest.raises(CCApplyError):
        apply_cc_edit(_DIFF_TWO_FILES, str(git_repo), gated_paths=["hello.txt"])
    assert not (git_repo / "EVIL.txt").exists()
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_apply_gated_paths_allows_vetted_files(git_repo):
    """Control: when every touched path IS vetted, the multi-file apply succeeds."""
    apply_cc_edit(_DIFF_TWO_FILES, str(git_repo), gated_paths=["hello.txt", "EVIL.txt"])
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello world\n"
    assert (git_repo / "EVIL.txt").read_text(encoding="utf-8") == "pwned\n"


# ── NB-W2 A1: codex_code scratch-write flows through the SAME cc_edit Gate ────
# The codex twin of the claude_code capture above. execute_node's state-aware
# codex_code block calls _maybe_capture_cc_edit (work-dir accessor generalized to
# CodexCodeClient._work_dir()); cc_edit.py (parser + gate + apply) is reused
# unchanged. Same three behaviors: park-for-human, in-scope auto-clear, no-diff.

@pytest.fixture
def patched_codex_client(monkeypatch, tmp_path):
    """Stub the codex_code spawn so execute_node's codex block returns a known
    response, and point CodexCodeClient._work_dir() at *tmp_path*."""
    from core.backends import codex_code as cx_mod

    workdir = tmp_path / "codex_work"
    workdir.mkdir()

    async def fake_chat(self, *, prompt, resume_session_id=None, posture=None):
        self.last_session_id = "thread-xyz"
        return {"text": "Proposed the edit (see work dir).", "session_id": "thread-xyz",
                "is_error": False, "api_error_status": None, "raw": {}}

    monkeypatch.setattr(cx_mod.CodexCodeClient, "chat", fake_chat)
    monkeypatch.setattr(
        cx_mod.CodexCodeClient, "_work_dir", lambda self: str(workdir)
    )
    import core.nodes.execute as ex
    import core.codex_sessions as sess
    from core.config import config

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ex, "_append_agent_turn_event", _noop)
    monkeypatch.setattr(sess, "_record_codex_session", _noop)
    monkeypatch.setattr(sess, "_lookup_codex_session", lambda *a, **k: _noop())
    # Same OFFLINE Gate as the claude fixture (no live critic call).
    monkeypatch.setattr(config, "GATE_CRITIC_BACKEND", "")
    return workdir


@pytest.mark.asyncio
async def test_codex_cc_edit_turn_emits_cc_edit_approval(patched_codex_client):
    from core.nodes.execute import execute_node

    workdir = patched_codex_client
    (workdir / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")

    state = {
        "task": "tweak hello",
        "backend": "codex_code",
        "messages": [],
        "conversation_id": "conv-cx-1",
        "codex_posture": {"mode": "scratch_write", "profile": "glm",
                          "scope": {"branch": "feat/x", "may_touch": ["hello.txt"]}},
    }
    out = await execute_node(state)

    assert out["approval_required"] is True
    assert out["approval_action_type"] == "cc_edit"
    details = out["approval_details"]
    assert details["file_path"] == "hello.txt"
    assert "+hello world" in details["diff"]
    assert details["conversation_id"] == "conv-cx-1"
    assert details["scope"] == {"branch": "feat/x", "may_touch": ["hello.txt"]}
    assert out["cc_pending_edit"]["diff"]
    # codex resume continuity still flows on the capture path.
    assert out["codex_resume_session_id"] == "thread-xyz"


@pytest.mark.asyncio
async def test_codex_cc_edit_gate_auto_applies_in_scope(
    patched_codex_client, monkeypatch, git_repo
):
    """In-scope codex cc_edit (cc_edit ∈ auto_actions + path ∈ may_touch) auto-clears
    the Gate and applies core-direct — same gate as claude, driven by a codex turn."""
    from core.config import config
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    workdir = patched_codex_client
    (workdir / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    state = {
        "task": "tweak hello", "backend": "codex_code", "messages": [],
        "conversation_id": "conv-cx-auto", "user_id": "admin",
        "codex_posture": {"mode": "scratch_write", "profile": "glm"},  # no scope
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["hello.txt"]},
    }
    out = await execute_node(state)
    assert out.get("approval_required") is False
    assert "approval_action_type" not in out
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello world\n"
    assert any("auto-applied by the Gate" in m["content"] for m in out.get("messages", []))


@pytest.mark.asyncio
async def test_codex_cc_edit_out_of_scope_parks(patched_codex_client, monkeypatch, git_repo):
    """An out-of-scope codex cc_edit (edited path NOT in may_touch) parks for a human
    and does NOT touch the tree, even with apply enabled."""
    from core.config import config
    from core.nodes.execute import execute_node

    monkeypatch.setattr(config, "CC_EDIT_APPLY_ENABLED", True)
    monkeypatch.setattr(config, "CC_PROJECT_DIR", str(git_repo))

    workdir = patched_codex_client
    (workdir / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")
    state = {
        "task": "tweak hello", "backend": "codex_code", "messages": [],
        "conversation_id": "conv-cx-oos", "user_id": "admin",
        "codex_posture": {"mode": "scratch_write", "profile": "glm"},
        # may_touch lists a DIFFERENT file → hello.txt is out of scope → human.
        "scope": {"auto_actions": ["cc_edit"], "may_touch": ["other.txt"]},
    }
    out = await execute_node(state)
    assert out["approval_required"] is True
    assert out["approval_action_type"] == "cc_edit"
    assert (git_repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"  # untouched


@pytest.mark.asyncio
async def test_codex_no_diff_is_normal_turn(patched_codex_client):
    """A codex scratch-write turn that proposes NO diff is a normal turn (no approval)."""
    from core.nodes.execute import execute_node

    state = {
        "task": "just answer", "backend": "codex_code", "messages": [],
        "conversation_id": "conv-cx-none",
        "codex_posture": {"mode": "scratch_write", "profile": "glm"},
    }
    out = await execute_node(state)
    assert out.get("approval_required") is False
    assert "approval_action_type" not in out
    assert out["messages"][0]["content"] == "Proposed the edit (see work dir)."


# ── F3: proposed_*.diff is consumed (unlinked) after capture ─────────────────

@pytest.mark.asyncio
async def test_cc_edit_proposed_diff_unlinked_after_capture(patched_cc_client):
    """F3: once a turn's proposed diff is captured into the approval item, the on-disk
    source is unlinked — so a LATER turn that proposes nothing new cannot re-capture (and
    re-apply) the stale diff."""
    import glob
    import os

    from core.nodes.execute import execute_node

    scratch = patched_cc_client
    (scratch / "proposed_1.diff").write_text(_DIFF_HELLO, encoding="utf-8")

    base = {
        "task": "tweak hello", "backend": "claude_code", "messages": [],
        "conversation_id": "conv-f3",
        "cc_posture": {"mode": "scratch_write", "scope": {"may_touch": ["hello.txt"]}},
    }
    out1 = await execute_node(dict(base))
    assert out1["approval_required"] is True                      # turn 1 captured
    # The source diff was consumed, not left behind.
    assert glob.glob(os.path.join(str(scratch), "proposed_*.diff")) == []

    # Turn 2: the CLI proposes nothing new → normal turn, NOT a re-capture of turn 1.
    out2 = await execute_node(dict(base))
    assert out2.get("approval_required") is False
    assert "approval_action_type" not in out2
