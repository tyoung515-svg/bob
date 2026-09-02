"""test_gist_format.py — the core suite enforces the gist-shape + landing standard.

Loads scripts/check_gists.py (it lives at the repo root, off the core import path) and:
  (1) asserts the FROZEN worked example EXAMPLE-GIST-0001.md passes as a valid gist;
  (2) asserts the schema-complete reference gist + valid landing pass;
  (3) for each violation fixture under tests/fixtures/gists/, asserts exactly that rule
      class trips with a precise, rule-tagged message (default-FAIL posture).

Nothing here mutates the real repo — every check is keyed off an explicit fixture path.
The standard being enforced is tasks/2026-07-07-gist-shapes/GIST-SHAPES.md §3/§4/§7.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKER_PATH = REPO_ROOT / "scripts" / "check_gists.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gists"
LANDINGS = FIXTURES / "landings"
EXAMPLE_GIST = REPO_ROOT / "tasks" / "2026-07-07-gist-shapes" / "EXAMPLE-GIST-0001.md"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_gists", _CHECKER_PATH)
    assert spec and spec.loader, f"cannot load checker at {_CHECKER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cg = _load_checker()


def _rules(findings) -> set[str]:
    return {f.rule for f in findings}


# ── (1) the frozen worked example passes as a valid gist ─────────────────────
def test_example_gist_passes():
    findings = cg.validate_gist_file(EXAMPLE_GIST)
    assert findings == [], [f.render() for f in findings]


def test_reference_gist_passes():
    findings = cg.validate_gist_file(FIXTURES / "0002-sample-capability.md")
    assert findings == [], [f.render() for f in findings]


def test_valid_landing_passes():
    findings = cg.validate_landing_file(LANDINGS / "landing_ok.md", gists_dir=FIXTURES)
    assert findings == [], [f.render() for f in findings]


# ── (2) one gist violation fixture per rule class — each fails precisely ──────
GIST_VIOLATIONS = [
    ("0010-missing-field.md", "missing-field", "title"),
    ("0011-bad-enum.md", "bad-enum", "criticality"),
    ("0012-id-mismatch.md", "id-filename", "does not agree"),
    ("0013-capability-path.md", "capability-path", "path-named"),
    ("0014-missing-heading.md", "missing-heading", "Scope fence"),
    ("0015-code-fence.md", "code-fence", "code-free"),
    ("0016-enterprise-distribution.md", "d17-distribution", "distribution"),
    ("0017-unknown-field.md", "unknown-field", "priority"),
]


@pytest.mark.parametrize("fname,rule,needle", GIST_VIOLATIONS)
def test_gist_violation_fixture_fails(fname, rule, needle):
    findings = cg.validate_gist_file(FIXTURES / fname)
    assert findings, f"{fname} should have failed but passed"
    # the intended rule class is present…
    assert rule in _rules(findings), (
        f"{fname}: expected rule {rule!r}, got {[f.render() for f in findings]}"
    )
    # …and its message is precise (names the offending thing).
    msgs = [f.message for f in findings if f.rule == rule]
    assert any(needle in m for m in msgs), (
        f"{fname}: rule {rule!r} message not precise about {needle!r}: {msgs}"
    )
    # exactly ONE rule class trips — each fixture is otherwise valid.
    assert _rules(findings) == {rule}, (
        f"{fname}: expected only {rule!r}, got {[f.render() for f in findings]}"
    )


# ── (3) default-FAIL posture: unknown field/enum is a REJECT, not a warn ──────
def test_default_fail_unknown_field_is_rejected():
    findings = cg.validate_gist_file(FIXTURES / "0017-unknown-field.md")
    assert any(f.rule == "unknown-field" for f in findings)


def test_default_fail_unknown_enum_is_rejected():
    findings = cg.validate_gist_file(FIXTURES / "0011-bad-enum.md")
    assert any(f.rule == "bad-enum" for f in findings)


# ── (4) landing violation fixtures — each fails precisely ─────────────────────
LANDING_VIOLATIONS = [
    ("landing_bad_tag.md", "landing-tag"),
    ("landing_bad_status.md", "landing-status"),
    ("landing_declined_no_reason.md", "landing-declined-reason"),
    ("landing_missing_invariant_row.md", "landing-invariant-row"),
    ("landing_no_table.md", "landing-table"),
]


@pytest.mark.parametrize("fname,rule", LANDING_VIOLATIONS)
def test_landing_violation_fixture_fails(fname, rule):
    findings = cg.validate_landing_file(LANDINGS / fname, gists_dir=FIXTURES)
    assert findings, f"{fname} should have failed but passed"
    assert rule in _rules(findings), (
        f"{fname}: expected rule {rule!r}, got {[f.render() for f in findings]}"
    )


# ── (5) CLI dir sweep: OK over the valid pair, non-zero over a violation ──────
def test_cli_dir_sweep_reports_violations(tmp_path):
    """validate_dir over a dir seeded with only the valid reference gist + valid landing
    is clean; adding a violation makes the CLI exit non-zero."""
    gd = tmp_path / "gists"
    (gd / "landings").mkdir(parents=True)
    (gd / "0002-sample-capability.md").write_text(
        (FIXTURES / "0002-sample-capability.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (gd / "landings" / "landing_ok.md").write_text(
        (LANDINGS / "landing_ok.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert cg.validate_dir(gd) == []
    assert cg.main([str(gd)]) == 0

    (gd / "0011-bad-enum.md").write_text(
        (FIXTURES / "0011-bad-enum.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert cg.main([str(gd)]) == 1
