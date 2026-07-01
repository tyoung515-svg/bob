"""
BoBClaw Core — Claude Code approved-edit path (C4)

The ``planner-cc-edit`` face (C2) runs the scratch-write posture (C2.1): it
reads the repo but can only WRITE to its per-conversation scratch dir
``CC_SCRATCH_ROOT/<conversation_id>``. Its system prompt instructs it to write
the proposed change as a unified diff to ``proposed_<n>.diff`` in that scratch
dir. The repo itself is write-protected, so the CLI never edits it.

C4 turns that proposed diff into a human-approved edit:

1. **Capture** — after a ``planner-cc-edit`` turn, read ``proposed_*.diff`` from
   the conversation's scratch dir (the same path ``ClaudeCodeClient`` derives).
   Fallbacks: an inline ```diff block in the reply, else nothing.
2. **Approve** — register ``cc_edit`` as an approval-requiring action and route
   it through :func:`route_approval` (the forward-compatible Gate seam). Today
   it always returns ``"human"`` → the existing T4 approvals inbox.
3. **Apply** — on approve, apply the stored diff to ``CC_PROJECT_DIR`` with a
   deterministic ``git apply`` (check-first, whole-or-nothing, no commit, no
   model in the write path). Gated by ``CC_EDIT_APPLY_ENABLED``.

This module owns the diff parser, the apply primitive, and the routing seam.
``execute_node`` wires them into the graph turn.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
from typing import Optional

from core.config import config

logger = logging.getLogger(__name__)

# A unified-diff header line that names the target file. We accept the common
# spellings the CLI / ``git diff`` emit: ``+++ b/path``, ``diff --git a/x b/x``.
_PLUS_HEADER = re.compile(r"^\+\+\+\s+(?:b/)?(.+?)\s*$", re.MULTILINE)
_GIT_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$", re.MULTILINE)

# Inline ```diff fenced block (fallback capture source).
_FENCED_DIFF = re.compile(
    r"```diff\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# A hunk header: ``@@ -oldStart[,oldCount] +newStart[,newCount] @@``. Counts default
# to 1 when ``,n`` is omitted. We count hunk-body lines off these so a ``-``/``+``
# *content* line inside a hunk is never mistaken for a file header — that mis-read is
# the "parser differential" the F1 audit named.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def split_unified_diff_by_file(body: str) -> list[dict]:
    """Split a (possibly multi-file) unified-diff *body* into one entry **per file**.

    Returns a list of ``{file_path, unified_diff}`` dicts — one per file section that
    carries a real hunk (``@@``). Sections with no hunk (pure mode/rename headers,
    prose) are dropped. Non-diff text yields ``[]``.

    This is the F1 fix: ``parse_unified_diff`` used to return the *whole* multi-file
    body as ONE entry whose ``file_path`` was only the FIRST ``+++ b/`` path, so a diff
    carrying ``in_scope.txt`` + ``EVIL.txt`` reported only ``in_scope.txt`` to the Gate —
    which cleared that one path and then ``git apply``-ed BOTH. Splitting per file makes
    the Gate see EVERY path (most-restrictive wins → the out-of-scope file parks).

    Section boundaries:
      * ``diff --git`` — always begins a new (git-style) section.
      * a ``--- ``/``+++ `` header pair — begins a new section ONLY when the current
        section is not already git-headed (a git section's own ``--- ``/``+++ `` lines
        belong to it, they are not a new boundary).
    Hunk bodies are consumed by counting old/new lines from the ``@@`` header, so a
    ``--- ``/``-`` deletion *content* line is never treated as a header. Pure: no I/O.
    """
    if not body:
        return []
    lines = body.splitlines(keepends=True)
    n = len(lines)
    sections: list[list[str]] = []
    cur: Optional[list[str]] = None
    cur_is_git = False
    i = 0
    while i < n:
        raw = lines[i]
        stripped = raw.rstrip("\r\n")
        if stripped.startswith("diff --git "):
            if cur is not None:
                sections.append(cur)
            cur, cur_is_git = [raw], True
            i += 1
            continue
        if (
            not cur_is_git
            and stripped.startswith("--- ")
            and i + 1 < n
            and lines[i + 1].rstrip("\r\n").startswith("+++ ")
        ):
            if cur is not None:
                sections.append(cur)
            cur, cur_is_git = [raw], False
            i += 1
            continue
        if cur is None:
            # Preamble before any file header (commit message, prose) — skip.
            i += 1
            continue
        m = _HUNK_RE.match(stripped)
        if m:
            cur.append(raw)
            old_rem = int(m.group(2)) if m.group(2) is not None else 1
            new_rem = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            while i < n and (old_rem > 0 or new_rem > 0):
                bl = lines[i]
                bl_stripped = bl.rstrip("\r\n")
                # Safety net: a hunk header that DECLARES more lines than it provides
                # (truncated / malformed / count-inflation attack) would otherwise make
                # this loop swallow the NEXT file's header as "hunk body", merging two
                # files into one section so the Gate sees only the first path. Stop at an
                # unambiguous new-file boundary instead. ``diff --git`` at column 0 can
                # never be hunk content (content lines lead with +/-/space); a plain
                # ``--- ``/``+++ `` pair is a boundary only outside a git section.
                if bl_stripped.startswith("diff --git ") or (
                    not cur_is_git
                    and bl_stripped.startswith("--- ")
                    and i + 1 < n
                    and lines[i + 1].rstrip("\r\n").startswith("+++ ")
                ):
                    break
                c = bl[:1]
                if c == "+":
                    new_rem -= 1
                elif c == "-":
                    old_rem -= 1
                elif c == "\\":
                    pass  # "\ No newline at end of file" — counts for neither side.
                else:
                    old_rem -= 1  # context (leading space) or a blank context line.
                    new_rem -= 1
                cur.append(bl)
                i += 1
            continue
        cur.append(raw)
        i += 1
    if cur is not None:
        sections.append(cur)

    out: list[dict] = []
    for sec in sections:
        text = "".join(sec)
        if "@@" not in text:
            continue
        out.append({
            "file_path": _file_path_from_diff(text),
            "unified_diff": text if text.endswith("\n") else text + "\n",
        })
    return out


def _file_path_from_diff(body: str) -> Optional[str]:
    """Best-effort extraction of the target file path from a unified diff body.

    Prefers the ``+++ b/<path>`` line (the post-image), then the
    ``diff --git a/x b/x`` header. ``/dev/null`` (pure deletion post-image) is
    skipped so we report the real path. Returns ``None`` if no path is found.
    """
    for m in _PLUS_HEADER.finditer(body):
        path = m.group(1).strip()
        if path and path != "/dev/null":
            return path
    m = _GIT_HEADER.search(body)
    if m:
        return m.group(2).strip()
    return None


def parse_unified_diff(body: str) -> Optional[dict]:
    """Parse a unified-diff *body* into the FIRST file's ``{file_path, unified_diff}``.

    Back-compat single-section view over :func:`split_unified_diff_by_file`. Returns
    ``None`` when the text carries no self-applicable file section (no ``@@`` hunk under
    a file header) — that is how prose / plan-file chatter is ignored.

    NOTE: for a MULTI-file body this returns only the first file. Capture goes through
    :func:`split_unified_diff_by_file` (one entry per file) so the Gate sees every path;
    do not use this for scope decisions on multi-file diffs.
    """
    sections = split_unified_diff_by_file(body)
    return sections[0] if sections else None


def parse_scratch_diffs(scratch_dir: str) -> list[dict]:
    """Read every ``proposed_*.diff`` in *scratch_dir*, newest-numbered first.

    Returns a list of ``{file_path, unified_diff}`` dicts (one per parseable
    file). Files that don't parse as a unified diff are skipped. A missing
    scratch dir yields ``[]``.
    """
    if not scratch_dir or not os.path.isdir(scratch_dir):
        return []
    paths = sorted(
        glob.glob(os.path.join(scratch_dir, "proposed_*.diff")),
        key=_proposed_sort_key,
    )
    out: list[dict] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            continue
        # F1: split per file so a multi-file proposed_*.diff yields one entry PER file —
        # the Gate then sees every target path, not just the first.
        out.extend(split_unified_diff_by_file(body))
    return out


def _proposed_sort_key(path: str):
    """Sort ``proposed_<n>.diff`` by the integer ``<n>`` (then name)."""
    name = os.path.basename(path)
    m = re.search(r"proposed_(\d+)\.diff$", name)
    return (int(m.group(1)) if m else 0, name)


def parse_inline_diffs(reply_text: str) -> list[dict]:
    """Fallback: extract ```diff fenced blocks from a reply (best-effort).

    A fenced block may itself carry several files — split per file (F1) so every
    target path reaches the Gate.
    """
    if not reply_text:
        return []
    out: list[dict] = []
    for m in _FENCED_DIFF.finditer(reply_text):
        out.extend(split_unified_diff_by_file(m.group(1)))
    return out


def capture_cc_edit(scratch_dir: str, reply_text: str) -> list[dict]:
    """Locate the proposed edit for a ``planner-cc-edit`` turn.

    Primary source is the deterministic scratch pad (``proposed_*.diff`` in
    *scratch_dir*); falls back to an inline ```diff block in *reply_text*.
    Returns ``[]`` when no diff is found (caller behaves like a normal planner
    turn — no approval).
    """
    diffs = parse_scratch_diffs(scratch_dir)
    if diffs:
        return diffs
    return parse_inline_diffs(reply_text)


# ── Gate-routable approval seam ──────────────────────────────────────────────

async def route_approval(action_type: str, details: dict) -> str:
    """Decide where an approval-requiring action goes: ``auto|gate|human``.

    This is the forward-compatible seam for the Gate router (see
    ``tasks/2026-06-15-gate-router/INTAKE.md``).

    When ``details["scope"]`` is present, consult the Gate policy in
    ``core.nodes.gate`` and return:

    * ``"auto"``  — action is within the spec's pre-approved scope → execute +
      audit-log, no human prompt.
    * ``"gate"``  — novel/ambiguous and no critic configured; surface for human.
    * ``"human"`` — out-of-scope/destructive/floor-match/critic-rejected →
      the operator decides.

    When no scope is provided, preserve current behaviour: return ``"human"``
    (the existing T4 inbox).

    Merge-to-main and the static ``_APPROVAL_REQUIRED`` floor stay
    always-``"human"`` regardless of scope.
    """
    scope_data = details.get("scope")
    if scope_data is None:
        return "human"

    from core.nodes.gate import gate_decide
    from core.permissions import Scope

    try:
        scope = Scope.model_validate(scope_data)
    except Exception:
        # Malformed scope: fail closed to human.
        return "human"

    critic_backend = details.get("critic_backend")
    decision = await gate_decide(action_type, details, scope, critic_backend)
    return decision.destination


# ── Apply primitive (core-direct git apply, no model) ────────────────────────

class CCApplyError(RuntimeError):
    """A ``cc_edit`` diff failed to apply cleanly (tree left untouched)."""


def _norm_diff_path(path: str) -> str:
    """Normalise a diff/numstat path for set comparison: forward slashes, no leading
    ``./``, stripped — so the splitter's ``+++ b/<p>`` path and git's numstat path match.
    """
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _git_apply_numstat_paths(patch: str, project_dir: str) -> list[str]:
    """The paths ``git apply`` reports it WILL touch — git's own authoritative parse.

    Uses ``git apply --numstat`` (git's diff parser, immune to the CRLF/prefix textual
    tricks a hand parser can trip on). Fails CLOSED: an unparseable patch, a missing
    ``git``, or a rename/copy (``a => b`` — not expected in a gated cc_edit) raises
    :class:`CCApplyError`, so nothing is applied.
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", "apply", "--numstat", "-"],
            input=patch,
            cwd=project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise CCApplyError(f"git not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CCApplyError(f"git apply --numstat timed out: {exc}") from exc
    if proc.returncode != 0:
        raise CCApplyError(
            f"git apply --numstat could not parse the diff (tree untouched): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel = parts[-1]
        if "=>" in rel:
            raise CCApplyError(
                f"refusing to apply: rename/copy not permitted in a gated cc_edit "
                f"({rel!r}; tree untouched)"
            )
        paths.append(rel)
    return paths


def apply_cc_edit(
    unified_diff: str,
    project_dir: Optional[str] = None,
    *,
    gated_paths: Optional[list] = None,
) -> None:
    """Apply *unified_diff* to *project_dir* via a deterministic ``git apply``.

    Whole-or-nothing: runs ``git apply --check`` first and only commits to the
    real apply when the check passes, so a diff that doesn't apply cleanly
    leaves the tree untouched (``git apply`` is atomic per-invocation). **Never
    commits** — the change lands in the working tree only.

    Path safety (F2 + F1 belt-and-braces) runs BEFORE the check: git's own
    ``--numstat`` gives the authoritative list of paths it will touch, and

    * every path must resolve INSIDE *project_dir* (``is_path_within`` resolves
      ``..``/symlinks on both sides first → no traversal / absolute / symlink escape);
    * when *gated_paths* is supplied (the Gate-vetted path set), git must not touch any
      path the Gate never saw — this kills the parser differential where a splitter
      mis-merge could smuggle an unvetted file past the scope check.

    Raises
    ------
    CCApplyError
        If the diff is empty, ``git`` is unavailable, a path escapes containment or the
        Gate, the check fails, or the apply fails. The message is safe to surface.
    """
    from core.permissions import is_path_within

    project_dir = project_dir or config.CC_PROJECT_DIR
    if not unified_diff or not unified_diff.strip():
        raise CCApplyError("empty diff — nothing to apply")
    patch = unified_diff if unified_diff.endswith("\n") else unified_diff + "\n"

    # 0. Path safety — derive git's authoritative target list, then contain + gate it.
    numstat_paths = _git_apply_numstat_paths(patch, project_dir)
    root = os.path.abspath(project_dir)
    for rel in numstat_paths:
        if not is_path_within(os.path.join(root, rel), root):
            raise CCApplyError(
                f"refusing to apply: {rel!r} resolves outside the project dir "
                f"(path containment; tree untouched)"
            )
    if gated_paths is not None:
        vetted = {_norm_diff_path(p) for p in gated_paths if p}
        for rel in numstat_paths:
            if _norm_diff_path(rel) not in vetted:
                raise CCApplyError(
                    f"refusing to apply: {rel!r} was not vetted by the Gate "
                    f"(scope-bypass guard; tree untouched)"
                )

    # 1. Dry-run check — fail here means the tree is never touched.
    check = _run_git_apply(["--check"], patch, project_dir)
    if check.returncode != 0:
        raise CCApplyError(
            f"git apply --check failed; diff does not apply cleanly "
            f"(tree untouched): {check.stderr.strip() or check.stdout.strip()}"
        )

    # 2. Real apply (no --index, no commit) — only reached when check passed.
    applied = _run_git_apply([], patch, project_dir)
    if applied.returncode != 0:
        raise CCApplyError(
            f"git apply failed after a clean check: "
            f"{applied.stderr.strip() or applied.stdout.strip()}"
        )


def _run_git_apply(
    extra_args: list[str], patch: str, project_dir: str
) -> subprocess.CompletedProcess:
    """Run ``git apply [extra_args]`` feeding *patch* on stdin from *project_dir*."""
    try:
        return subprocess.run(
            ["git", "apply", *extra_args, "-"],
            input=patch,
            cwd=project_dir,
            capture_output=True,
            text=True,
            # Force UTF-8 for stdin/stdout: real CC diffs contain non-ASCII
            # (em-dashes, etc.). Without this, subprocess uses the platform
            # default (cp1252 on Windows), the stdin writer thread crashes on an
            # un-encodable char, git never receives the patch, and `git apply -`
            # blocks until the timeout. (Caught by the C4 live E2E, 2026-06-15.)
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise CCApplyError(f"git not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CCApplyError(f"git apply timed out: {exc}") from exc
