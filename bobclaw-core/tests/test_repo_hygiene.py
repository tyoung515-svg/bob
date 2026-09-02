"""test_repo_hygiene.py — the core suite enforces repo hygiene FOREVER.

Loads scripts/check_hygiene.py (it lives at the repo root, off the core import path)
and (1) asserts the checker is CLEAN on the current tree, then (2) for each of the five
checks (a-e) seeds a deliberate violation in a tmp_path sandbox and asserts exactly that
check trips with the right severity / a non-zero CLI exit. Nothing here mutates the real
repo — every check is keyed off an explicit root Path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKER_PATH = REPO_ROOT / "scripts" / "check_hygiene.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_hygiene", _CHECKER_PATH)
    assert spec and spec.loader, f"cannot load checker at {_CHECKER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    # register before exec so @dataclass can resolve cls.__module__ in sys.modules
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hy = _load_checker()


def _mk_secrets(tmp_path: Path, name: str, content: str) -> Path:
    sd = tmp_path / hy.SANCTIONED_SECRET_DIR
    sd.mkdir(exist_ok=True)
    p = sd / name
    p.write_text(content, encoding="utf-8")
    return p


# ── (0) the checker is clean on the current post-H1 tree ─────────────────────
def test_checker_clean_on_current_tree():
    findings = hy.run_all_checks(REPO_ROOT)
    fails = [f for f in findings if f.is_fail]
    assert fails == [], f"hygiene FAILs on the current tree: {[f.render() for f in fails]}"


def test_main_exits_zero_on_current_tree():
    assert hy.main(["--root", str(REPO_ROOT)]) == 0


def test_checks_are_cwd_independent(tmp_path, monkeypatch):
    """No check reads the process CWD — git is scoped with `git -C root`."""
    monkeypatch.chdir(tmp_path)
    findings = hy.run_all_checks(REPO_ROOT)
    assert [f for f in findings if f.is_fail] == []


# ── (a) secret-shaped files ──────────────────────────────────────────────────
def test_check_a_untracked_secret_at_root_trips(tmp_path):
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    findings = hy.check_secret_shaped_files(tmp_path, tracked=[])
    assert any(f.check == "a" and f.is_fail for f in findings)


def test_check_a_pem_and_token_at_root_trip(tmp_path):
    (tmp_path / "server.pem").write_text("x", encoding="utf-8")
    (tmp_path / "api_token.txt").write_text("x", encoding="utf-8")
    findings = hy.check_secret_shaped_files(tmp_path, tracked=[])
    fails = [f for f in findings if f.check == "a" and f.is_fail]
    assert len(fails) == 2


def test_check_a_tracked_secret_anywhere_trips(tmp_path):
    findings = hy.check_secret_shaped_files(
        tmp_path, tracked=["bobclaw-gateway/deep/access_token.txt"]
    )
    assert any(f.check == "a" and f.is_fail for f in findings)


def test_check_a_secrets_dir_is_excluded(tmp_path):
    """A secret UNDER .secrets/ is CORRECT — it must never trip (on disk OR tracked)."""
    _mk_secrets(tmp_path, "credentials.json", "{}")
    _mk_secrets(tmp_path, "cli-token.txt", "abc")
    findings = hy.check_secret_shaped_files(
        tmp_path,
        tracked=[".secrets/credentials.json", ".secrets/cli-token.txt"],
    )
    assert findings == []


def test_check_a_untracked_subdir_secret_not_flagged(tmp_path):
    """Scope fence: only ROOT (untracked/tracked) or tracked-anywhere is in scope."""
    sub = tmp_path / "scripts"
    sub.mkdir()
    (sub / "leaked_token.txt").write_text("x", encoding="utf-8")
    findings = hy.check_secret_shaped_files(tmp_path, tracked=[])
    assert findings == []


# ── (b) committed artifacts inside a service package root ─────────────────────
def test_check_b_tracked_db_in_service_root_trips(tmp_path):
    findings = hy.check_service_artifacts(
        tmp_path, tracked=["bobclaw-core/bobclaw_cache.db"]
    )
    assert any(f.check == "b" and f.is_fail for f in findings)


def test_check_b_report_and_audit_trip(tmp_path):
    findings = hy.check_service_artifacts(
        tmp_path,
        tracked=[
            "bobclaw-core/WORKER_INT_3_A_REPORT.md",
            "bobclaw-gateway/audit_report.md",
        ],
    )
    fails = [f for f in findings if f.check == "b" and f.is_fail]
    assert len(fails) == 2


def test_check_b_nonservice_and_source_not_flagged(tmp_path):
    findings = hy.check_service_artifacts(
        tmp_path,
        tracked=[
            "data/foo.db",  # not under a service root
            "tasks/x.db",  # not under a service root
            "bobclaw-core/core/nodes/route.py",  # normal source
            "bobclaw-core/tests/test_teams.py",
        ],
    )
    assert findings == []


# ── (c) root-doc ceiling ─────────────────────────────────────────────────────
def test_check_c_over_ceiling_trips(tmp_path):
    for i in range(5):
        (tmp_path / f"doc{i}.md").write_text("#", encoding="utf-8")
    findings = hy.check_root_doc_count(tmp_path, max_docs=3)
    assert any(f.check == "c" and f.is_fail for f in findings)


def test_check_c_at_or_under_ceiling_ok(tmp_path):
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text("#", encoding="utf-8")
    assert hy.check_root_doc_count(tmp_path, max_docs=3) == []


def test_check_c_ignores_nested_md(tmp_path):
    """Only *.md directly at root count — docs/archive/*.md must not."""
    (tmp_path / "a.md").write_text("#", encoding="utf-8")
    nested = tmp_path / "docs" / "archive"
    nested.mkdir(parents=True)
    for i in range(10):
        (nested / f"n{i}.md").write_text("#", encoding="utf-8")
    assert hy.check_root_doc_count(tmp_path, max_docs=2) == []


# ── (d) env-file lint ────────────────────────────────────────────────────────
def test_check_d_inline_comment_poison_trips(tmp_path):
    """The exact tonight's-bug shape: VAR= #comment silently blanks the var."""
    _mk_secrets(tmp_path, "bobclaw.env", "KIMI_CLI_PATH= #/usr/local/bin/kimi\n")
    findings = hy.check_env_files(tmp_path)
    fails = [f for f in findings if f.check == "d" and f.is_fail]
    assert len(fails) == 1
    assert "KIMI_CLI_PATH" in fails[0].message
    # never leaks the value into the report
    assert "/usr/local/bin/kimi" not in fails[0].message


def test_check_d_poison_no_space_trips(tmp_path):
    _mk_secrets(tmp_path, "bobclaw.env", "FOO=#hidden\n")
    fails = [f for f in hy.check_env_files(tmp_path) if f.is_fail]
    assert len(fails) == 1


def test_check_d_legit_inline_comment_not_flagged(tmp_path):
    """A value that is PRESENT then a trailing comment is legit — must not trip."""
    _mk_secrets(
        tmp_path,
        "bobclaw.env",
        "KIMI_CLI_PATH=/usr/local/bin/kimi  # the real path\n"
        'QUOTED="#notacomment"\n'
        "SPACED= /real/path  # ok\n"
        "# WHOLE_LINE=comment\n"
        "\n",
    )
    assert [f for f in hy.check_env_files(tmp_path) if f.is_fail] == []


def test_check_d_empty_keylike_is_info_not_fail(tmp_path):
    _mk_secrets(tmp_path, "bobclaw.env", "DEEPSEEK_API_KEY=\nTOTP_SECRET=   \n")
    findings = hy.check_env_files(tmp_path)
    assert [f for f in findings if f.is_fail] == []
    infos = [f for f in findings if f.check == "d" and f.severity == hy.INFO]
    assert len(infos) == 2


def test_check_d_only_scans_dotenv_glob(tmp_path):
    """.env.example (not *.env) is out of scope."""
    _mk_secrets(tmp_path, "bobclaw.env.example", "FOO= #poison-but-example\n")
    assert hy.check_env_files(tmp_path) == []


# ── (e) tasks/** untracked run-artifact accretion (WARN, never FAIL) ──────────
def test_check_e_over_threshold_warns_not_fails(tmp_path):
    findings = hy.check_tasks_artifacts(tmp_path, untracked_count=200, threshold=100)
    assert any(f.check == "e" and f.severity == hy.WARN for f in findings)
    assert [f for f in findings if f.is_fail] == []


def test_check_e_under_threshold_ok(tmp_path):
    assert hy.check_tasks_artifacts(tmp_path, untracked_count=5, threshold=100) == []


# ── CLI-level non-zero exit on a seeded FAIL ─────────────────────────────────
def test_main_nonzero_exit_on_seeded_violation(tmp_path):
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    assert hy.main(["--root", str(tmp_path)]) == 1


def test_main_fix_flag_never_mutates(tmp_path):
    """--fix prints guidance only — it must not create/delete/edit any file."""
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())
    rc = hy.main(["--root", str(tmp_path), "--fix"])
    after = sorted(p.name for p in tmp_path.iterdir())
    assert rc == 1  # still reports the violation
    assert before == after  # nothing added or removed


def test_main_nonzero_exit_on_root_doc_overflow(tmp_path):
    """CLI-exit for check (c): main() returns 1 when the on-disk root-doc ceiling is blown."""
    for i in range(hy.MAX_ROOT_DOCS + 1):
        (tmp_path / f"d{i}.md").write_text("#", encoding="utf-8")
    assert hy.main(["--root", str(tmp_path)]) == 1


def test_main_nonzero_exit_on_env_poison(tmp_path):
    """CLI-exit for check (d): main() returns 1 on an inline-comment-poisoned env var."""
    _mk_secrets(tmp_path, "bobclaw.env", "KIMI_CLI_PATH= #/usr/bin/kimi\n")
    assert hy.main(["--root", str(tmp_path)]) == 1


def test_main_nonzero_exit_on_tracked_service_artifact(tmp_path):
    """CLI-exit for check (b) through main()'s REAL git-tracked derivation: a service-root
    artifact staged in a git index makes main() (which derives `tracked` via `git -C root
    ls-files`) return 1. Closes the 'a dropped (b) FAIL at main() level' audit concern
    end-to-end, not just at the run_all_checks aggregation level."""
    import shutil
    import subprocess

    if shutil.which("git") is None:  # pragma: no cover - git is a repo dependency
        pytest.skip("git not available")

    def _git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    _git("init")
    svc = tmp_path / "bobclaw-core"
    svc.mkdir()
    (svc / "bobclaw_cache.db").write_text("x", encoding="utf-8")
    _git("add", "bobclaw-core/bobclaw_cache.db")  # staged => git ls-files sees it as tracked
    # only check (b) can trip here (no root secret, no .md, no .env), so exit 1 is (b)'s.
    assert hy.main(["--root", str(tmp_path)]) == 1


def test_run_all_checks_aggregates_every_check(tmp_path):
    """run_all_checks must call AND aggregate all five checks — seed one violation each
    and assert a FAIL surfaces for a/b/c/d and a WARN for e (the aggregation main() gates
    its exit on). Guards against a check being silently dropped from the orchestrator."""
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")  # (a)
    for i in range(hy.MAX_ROOT_DOCS + 1):  # (c)
        (tmp_path / f"d{i}.md").write_text("#", encoding="utf-8")
    _mk_secrets(tmp_path, "bobclaw.env", "FOO= #poison\n")  # (d)
    findings = hy.run_all_checks(
        tmp_path,
        tracked=["bobclaw-core/bobclaw_cache.db"],  # (b)
        untracked_tasks_count=200,  # (e)
    )
    checks_with_fail = {f.check for f in findings if f.is_fail}
    assert checks_with_fail == {"a", "b", "c", "d"}
    assert any(f.check == "e" and f.severity == hy.WARN for f in findings)
