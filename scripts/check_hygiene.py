#!/usr/bin/env python3
"""check_hygiene.py — BoBClaw repo hygiene guardrail (stdlib-only).

The durable "never again" enforcement for the mess the 2026-07-07 review found
(a secret-shaped file loose at the repo root, committed DB/report artifacts inside
service package roots, root-doc sprawl, an inline-comment-poisoned env file that
silently blanked four *_CLI_PATH vars, and unbounded tasks/** run-artifact accretion).

Rules live in REPO_HYGIENE.md. This script is the machine that enforces them; it is
also run in-suite by bobclaw-core/tests/test_repo_hygiene.py so the core test suite
enforces hygiene forever, and it wires as a pre-commit local hook.

Design invariants (do not regress):
  * STDLIB ONLY. No third-party imports — must run in a bare Python 3.11+ interpreter.
  * EXIT NON-ZERO on any hard violation (a FAIL); WARN / INFO never fail the run.
  * NO CWD / GLOBAL STATE. Every check keys off an explicit `root` Path (and git data
    computed with `git -C <root>`), so it is correct under BoBClaw's multi-process model
    and testable against a tmp_path sandbox without touching the real repo.
  * --fix is CONSERVATIVE: it prints remediation guidance only. It NEVER deletes a file
    and NEVER mutates tracked content — that (deletion) is the human's call. This is the
    scope fence: the checker WARNS/reports, it does not clean up for you.

Each of the five checks is a pure function taking `root` (+ pre-computed git data where
needed) and returning a list of Findings, so tests can seed a violation in isolation.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# ── severities ───────────────────────────────────────────────────────────────
FAIL = "FAIL"   # a hard violation → non-zero exit
WARN = "WARN"   # surfaced, does not fail the run
INFO = "INFO"   # advisory only


@dataclass(frozen=True)
class Finding:
    """One hygiene observation. `check` is the a–e id from REPO_HYGIENE.md."""

    check: str
    severity: str
    message: str
    path: str = ""

    @property
    def is_fail(self) -> bool:
        return self.severity == FAIL

    def render(self) -> str:
        loc = f" [{self.path}]" if self.path else ""
        return f"{self.severity:<4} ({self.check}) {self.message}{loc}"


# ── configuration constants (the allowlist / ceilings live HERE, in the script) ──

# (a) secret-shaped filename globs. Matched by basename at the repo root (any tracked
#     status — an UNTRACKED secret at root is the exposure vector that caused the
#     incident) and against every git-tracked path anywhere. The sanctioned .secrets/
#     dir is excluded (it is gitignored; a secret there is CORRECT).
SECRET_SHAPE_GLOBS: tuple[str, ...] = ("credentials*.json", "*.pem", "*token*.txt")
SANCTIONED_SECRET_DIR = ".secrets"

# (b) no build/report/DB artifacts committed inside a service package root.
SERVICE_ROOTS: tuple[str, ...] = (
    "bobclaw-core",
    "bobclaw-gateway",
    "bobclaw-claude-pipeline",
    "bobclaw-app",
)
SERVICE_ARTIFACT_GLOBS: tuple[str, ...] = ("*.db", "audit_report*", "*_REPORT.md")

# (c) root-level *.md ceiling.
# NOTE: post-archive KEEP set (MS5-H3, 2026-07-07). H3 archived the movable history docs
# into docs/archive/ and strategy docs into docs/strategy/, leaving the committed KEEP set at
# root: CLAUDE.md + AGENTS.md (tracked retirement stub) + bobclaw-unified-architecture-spec.md
# + REPO_HYGIENE.md + SERVED_AGENT_STRATEGY.md + 9x FABLE_AUDIT_* (audited/frozen, must stay
# at root) = exactly 14 tracked root *.md, identical on Travis's tree AND a fresh clone. The
# 9 frozen FABLE_AUDIT docs set a HARD FLOOR of 14, so this ceiling EXACTLY matches the
# committed KEEP set (14) — not the ~12 the H2 hand-off estimated before counting the FABLE
# set. Lowered from 30 by H3 in the same commit as the archive move.
MAX_ROOT_DOCS = 14

# (d) env-file lint: which .secrets files to scan, and which var names are "key-like"
#     (empty value → INFO). The poison pattern (VAR=<ws>#comment) is always a FAIL.
ENV_GLOB = "*.env"
KEYLIKE_SUFFIXES: tuple[str, ...] = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PATH",
    "_PASSWORD",
    "_ID",
    "_URL",
)

# (e) untracked tasks/** run-artifact WARNING threshold (warn, never fail).
TASKS_ARTIFACT_WARN_THRESHOLD = 100

# env-line parse: optional leading `export`, a var name, `=`, then the value (rest of line).
_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# poison: the value is (optional whitespace then) an inline comment `#...` — a dotenv
# parser reads this as an empty/comment value, silently blanking the variable.
_ENV_POISON_RE = re.compile(r"^\s*#")


# ── git helpers (all scoped to `root`, never the process CWD) ─────────────────
def _git_lines(root: Path, *args: str) -> list[str]:
    """Run `git -C root <args>` and return stdout lines. Fail-open to [] if git is
    unavailable or `root` is not a repo — the on-disk checks still run."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return []
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def git_tracked(root: Path) -> list[str]:
    """Repo-relative paths of every git-tracked file (forward-slash separated)."""
    return _git_lines(root, "ls-files")


def git_untracked_tasks_count(root: Path) -> int:
    """Count untracked (non-ignored) run artifacts under tasks/."""
    return len(_git_lines(root, "ls-files", "--others", "--exclude-standard", "tasks/"))


def _under_sanctioned_secret_dir(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    return SANCTIONED_SECRET_DIR in parts


def _matches_any(name: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


# ── (a) secret-shaped files ───────────────────────────────────────────────────
def check_secret_shaped_files(root: Path, tracked: list[str]) -> list[Finding]:
    """FAIL on a secret-shaped file present at the repo ROOT (tracked OR untracked —
    untracked-at-root is the exposure vector) or tracked ANYWHERE. Excludes .secrets/."""
    findings: list[Finding] = []
    seen: set[str] = set()

    # untracked-or-tracked, present at the repo root (top level only; .secrets/ excluded
    # because it is a subdir, not a root-level entry).
    for entry in sorted(root.iterdir() if root.is_dir() else []):
        if entry.is_file() and _matches_any(entry.name, SECRET_SHAPE_GLOBS):
            rel = entry.name
            seen.add(rel)
            findings.append(
                Finding(
                    "a",
                    FAIL,
                    f"secret-shaped file at repo root: {entry.name} "
                    f"(move it into {SANCTIONED_SECRET_DIR}/ and rotate the secret)",
                    path=rel,
                )
            )

    # tracked anywhere in the repo (excluding the sanctioned .secrets/ dir).
    for rel in tracked:
        rel_norm = rel.replace("\\", "/")
        if _under_sanctioned_secret_dir(rel_norm):
            continue
        name = rel_norm.rsplit("/", 1)[-1]
        if _matches_any(name, SECRET_SHAPE_GLOBS) and rel_norm not in seen:
            seen.add(rel_norm)
            findings.append(
                Finding(
                    "a",
                    FAIL,
                    f"secret-shaped file tracked in git: {rel_norm} "
                    f"(git rm --cached it, move into {SANCTIONED_SECRET_DIR}/, rotate)",
                    path=rel_norm,
                )
            )
    return findings


# ── (b) committed artifacts inside a service package root ─────────────────────
def check_service_artifacts(root: Path, tracked: list[str]) -> list[Finding]:
    """FAIL on a tracked *.db / audit_report* / *_REPORT.md anywhere under a service root."""
    findings: list[Finding] = []
    for rel in tracked:
        rel_norm = rel.replace("\\", "/")
        top = rel_norm.split("/", 1)[0]
        if top not in SERVICE_ROOTS:
            continue
        name = rel_norm.rsplit("/", 1)[-1]
        if _matches_any(name, SERVICE_ARTIFACT_GLOBS):
            findings.append(
                Finding(
                    "b",
                    FAIL,
                    f"build/report/DB artifact committed in a service root: {rel_norm} "
                    f"(git rm --cached it; it is regenerable output, not source)",
                    path=rel_norm,
                )
            )
    return findings


# ── (c) root-doc ceiling ──────────────────────────────────────────────────────
def check_root_doc_count(root: Path, max_docs: int = MAX_ROOT_DOCS) -> list[Finding]:
    """FAIL when the number of *.md files directly at the repo root exceeds `max_docs`."""
    if not root.is_dir():
        return []
    docs = sorted(p.name for p in root.glob("*.md") if p.is_file())
    if len(docs) > max_docs:
        return [
            Finding(
                "c",
                FAIL,
                f"root-level *.md count {len(docs)} exceeds ceiling {max_docs} - "
                f"move history/strategy docs into docs/archive/ or docs/strategy/",
                path=f"{len(docs)} root docs",
            )
        ]
    return []


# ── (d) env-file lint over .secrets/*.env ─────────────────────────────────────
def check_env_files(root: Path) -> list[Finding]:
    """Lint .secrets/*.env. The inline-comment-poison pattern (VAR=<ws>#comment) is a
    FAIL. A key-like var with an EMPTY value is INFO only. Never prints a value."""
    findings: list[Finding] = []
    secrets_dir = root / SANCTIONED_SECRET_DIR
    if not secrets_dir.is_dir():
        return findings
    for env_path in sorted(secrets_dir.glob(ENV_GLOB)):
        if not env_path.is_file():
            continue
        try:
            text = env_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f"{SANCTIONED_SECRET_DIR}/{env_path.name}"
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue  # blank or full-line comment
            m = _ENV_ASSIGN_RE.match(raw)
            if not m:
                continue
            name, value = m.group(1), m.group(2)
            if _ENV_POISON_RE.match(value):
                # value is whitespace-then-'#': the assignment silently resolves to a
                # comment / empty string. Report NAME + line only — never the value.
                findings.append(
                    Finding(
                        "d",
                        FAIL,
                        f"inline-comment-poisoned env var {name} "
                        f"(value is a '#'-comment -> silently blanked) at {rel}:{lineno}",
                        path=rel,
                    )
                )
                continue
            if value.strip() == "" and name.endswith(KEYLIKE_SUFFIXES):
                findings.append(
                    Finding(
                        "d",
                        INFO,
                        f"key-like env var {name} has an empty value at {rel}:{lineno}",
                        path=rel,
                    )
                )
    return findings


# ── (e) tasks/** untracked run-artifact accretion ─────────────────────────────
def check_tasks_artifacts(
    root: Path, untracked_count: int, threshold: int = TASKS_ARTIFACT_WARN_THRESHOLD
) -> list[Finding]:
    """WARN (never FAIL) when untracked tasks/** run artifacts exceed `threshold`."""
    if untracked_count > threshold:
        return [
            Finding(
                "e",
                WARN,
                f"{untracked_count} untracked tasks/** run artifacts exceed the warn "
                f"threshold {threshold} - add ignore patterns or prune old sprint scratch",
                path="tasks/",
            )
        ]
    return []


# ── orchestration ─────────────────────────────────────────────────────────────
def run_all_checks(
    root: Path,
    *,
    tracked: Optional[list[str]] = None,
    untracked_tasks_count: Optional[int] = None,
) -> list[Finding]:
    """Run every check against `root`. Git-derived inputs are computed once here (or may
    be injected by a caller/test). Returns all findings across the five checks."""
    root = Path(root)
    if tracked is None:
        tracked = git_tracked(root)
    if untracked_tasks_count is None:
        untracked_tasks_count = git_untracked_tasks_count(root)
    findings: list[Finding] = []
    findings += check_secret_shaped_files(root, tracked)
    findings += check_service_artifacts(root, tracked)
    findings += check_root_doc_count(root)
    findings += check_env_files(root)
    findings += check_tasks_artifacts(root, untracked_tasks_count)
    return findings


def _repo_root() -> Path:
    """This script lives at <repo>/scripts/check_hygiene.py."""
    return Path(__file__).resolve().parents[1]


_FIX_GUIDANCE = {
    "a": "Move the secret into .secrets/ (gitignored) and ROTATE it; if tracked, "
    "`git rm --cached <path>` and add a .gitignore rule.",
    "b": "`git rm --cached <path>` — it is regenerable output; add its glob to .gitignore.",
    "c": "`git mv` history docs to docs/archive/ and strategy docs to docs/strategy/, "
    "then lower MAX_ROOT_DOCS in scripts/check_hygiene.py.",
    "d": "Fix the env line: put the value BEFORE the '#', e.g. `VAR=/real/path  # note` "
    "(a value that is only '#...' is read as empty).",
    "e": "Add ignore patterns for the run captures (e.g. tasks/**/_*.out) or prune old "
    "sprint scratch. Nothing is deleted automatically.",
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BoBClaw repo hygiene guardrail")
    parser.add_argument(
        "--root",
        type=Path,
        default=_repo_root(),
        help="repo root to check (default: the repo this script lives in)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="print CONSERVATIVE remediation guidance (never deletes or edits files)",
    )
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    findings = run_all_checks(root)

    fails = [f for f in findings if f.severity == FAIL]
    warns = [f for f in findings if f.severity == WARN]
    infos = [f for f in findings if f.severity == INFO]

    to_show = fails if args.quiet else findings
    for f in sorted(to_show, key=lambda x: (x.check, x.severity)):
        print(f.render())

    if args.fix:
        checks_hit = sorted({f.check for f in findings})
        if checks_hit:
            print("\n--fix guidance (nothing was deleted or edited):")
            for c in checks_hit:
                print(f"  ({c}) {_FIX_GUIDANCE.get(c, 'see REPO_HYGIENE.md')}")

    print(
        f"\nhygiene: {len(fails)} FAIL, {len(warns)} WARN, {len(infos)} INFO "
        f"(root={root})"
    )
    if fails:
        print("HYGIENE CHECK FAILED - see REPO_HYGIENE.md")
        return 1
    print("hygiene OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
