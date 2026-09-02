"""MS8-SC3 — stack auditor GOLDEN + EST-framing acceptance (L2-authored).

Data-bound acceptance test: the fixture telemetry + subscriptions YAML -> a frozen golden
usage-vs-subscription report, plus the load-bearing COST-2 assertion — EVERY savings/cost figure
in the report carries the EST badge (structural, rendered, and per-figure). Import the submodule
directly (core.scout.__init__ is shared with SC2 and is NOT modified by SC3).
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from core.scout.stack_audit import (
    Money,
    Recommendation,
    StackAuditReport,
    audit_stack,
    load_subscriptions,
    load_usage_telemetry,
)

FIXTURES = Path(__file__).parent / "fixtures"
TELEMETRY = FIXTURES / "stack_audit_telemetry.yaml"
SUBSCRIPTIONS = FIXTURES / "stack_audit_subscriptions.yaml"
GOLDEN_YAML = FIXTURES / "stack_audit_report_golden.yaml"
GOLDEN_MD = FIXTURES / "stack_audit_report_golden.md"

# Frozen so the golden is reproducible (audit_stack is otherwise deterministic).
GENERATED_AT = "2026-07-07T00:00:00Z"


@pytest.fixture
def report() -> StackAuditReport:
    telemetry = load_usage_telemetry(TELEMETRY)
    subscriptions = load_subscriptions(SUBSCRIPTIONS)
    return audit_stack(telemetry, subscriptions, generated_at=GENERATED_AT)


# ── golden ───────────────────────────────────────────────────────────────────
def test_report_matches_golden_dict(report):
    golden = yaml.safe_load(GOLDEN_YAML.read_text(encoding="utf-8"))
    assert report.to_dict() == golden


def test_report_matches_golden_markdown(report):
    golden_md = GOLDEN_MD.read_text(encoding="utf-8")
    assert report.to_markdown() == golden_md


def test_golden_yaml_reloads_and_equals_report(report):
    # the frozen golden YAML round-trips back into an equal report (schema-valid golden)
    reloaded = StackAuditReport.from_yaml(GOLDEN_YAML)
    assert reloaded == report


# ── the fixture drives every recommendation branch (evidence the golden is real) ──
def test_fixture_exercises_all_recommendations(report):
    by_id = {f.subscription_id: f for f in report.findings}
    # findings sorted by subscription_id
    assert [f.subscription_id for f in report.findings] == sorted(by_id)
    assert by_id["claude-max"].recommendation == Recommendation.KEEP
    assert by_id["kimi-membership"].recommendation == Recommendation.DOWNGRADE
    assert by_id["gemini-advanced"].recommendation == Recommendation.CANCEL
    assert by_id["chatgpt-plus"].recommendation == Recommendation.REVIEW
    # savings framed EST and numerically right
    assert by_id["gemini-advanced"].est_savings.usd_month == 20.0      # full monthly (unused)
    assert by_id["kimi-membership"].est_savings.usd_month == 40.0      # 60 - 20 downgrade delta
    assert by_id["claude-max"].est_savings.usd_month == 0.0            # keep
    assert by_id["chatgpt-plus"].est_savings.known is False            # not measurable
    # aggregate: known savings only (excludes the REVIEW-unknown), with a disclosure note
    assert report.total_monthly_cost.usd_month == 300.0
    assert report.total_est_savings.usd_month == 60.0
    assert "excludes 1 unquantified REVIEW item" in report.total_est_savings.note


def test_unmapped_backends_surface_payg_and_local(report):
    unmapped = {u.backend: u for u in report.unmapped_backends}
    # PAYG spend BoB routes but no subscription covers, + local (no metered cost)
    assert set(unmapped) == {"claude_api", "deepseek_v4_flash", "minimax", "local"}
    assert unmapped["claude_api"].metered_cost.usd_month == 4.80
    assert unmapped["local"].note == "local — no metered cost"
    assert unmapped["minimax"].note == "PAYG / not covered by a subscription"


# ── THE load-bearing COST-2 acceptance: every figure carries the EST badge ──────
def _walk(obj):
    """Yield every dict/list node in a nested structure (depth-first)."""
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def test_est_framing_structural_every_money_dict_is_badged(report):
    """STRUCTURAL: every serialized money object (any dict carrying 'usd_month') is EST-framed."""
    d = report.to_dict()
    money_nodes = [n for n in _walk(d) if isinstance(n, dict) and "usd_month" in n]
    # the report has many money figures (per-finding cost + savings + metered, totals, unmapped)
    assert len(money_nodes) >= 12
    for node in money_nodes:
        assert node.get("framing") == "EST", f"unbadged money figure in report: {node}"


def test_est_framing_rendered_every_dollar_line_is_badged(report):
    """RENDERED: in the chat markdown, EVERY line that shows a '$' also shows '(EST)'."""
    md = report.to_markdown()
    dollar_lines = [ln for ln in md.splitlines() if "$" in ln]
    assert dollar_lines, "expected the report to contain dollar figures"
    for line in dollar_lines:
        assert "(EST)" in line, f"dollar figure without EST badge: {line!r}"


def test_est_framing_per_figure_badges_end_in_est(report):
    """PER-FIGURE: every Money.badge in the report ends with '(EST)' and framing == 'EST'."""
    monies: list[Money] = [report.total_monthly_cost, report.total_est_savings]
    for f in report.findings:
        monies += [f.monthly_cost, f.est_savings, f.usage.metered_cost]
    for u in report.unmapped_backends:
        monies.append(u.metered_cost)
    assert len(monies) >= 12
    for m in monies:
        assert m.framing == "EST"
        assert m.badge.endswith("(EST)"), f"badge not EST-terminated: {m.badge!r}"


def test_no_bare_dollar_precision_leak(report):
    """COST-2 'no fake precision': money renders to cents and always through the EST badge —
    a savings/cost figure never appears as a bare '$N' without the estimate marker."""
    md = report.to_markdown()
    # every '$' occurrence is inside a "~$....(EST)" badge context on its line
    for line in md.splitlines():
        if "$" in line:
            assert "~$" in line and "(EST)" in line


# ── audit-round hardening (r1 advisory concerns) ───────────────────────────────
def test_audit_stack_does_not_mutate_inputs():
    """audit_stack reads telemetry/subscriptions READ-ONLY — it must not mutate its inputs
    (the telemetry is the live routing/cost ledger's rollup; mutation would be a data bug)."""
    telemetry = load_usage_telemetry(TELEMETRY)
    subscriptions = load_subscriptions(SUBSCRIPTIONS)
    tel_before = telemetry.to_dict()
    subs_before = subscriptions.to_dict()
    audit_stack(telemetry, subscriptions, generated_at=GENERATED_AT)
    assert telemetry.to_dict() == tel_before, "audit_stack mutated the telemetry input"
    assert subscriptions.to_dict() == subs_before, "audit_stack mutated the subscriptions input"


def test_markdown_surfaces_disclosure_notes(report):
    """The rendered report must not silently hide context: the annual-normalization note and the
    'excludes N unquantified REVIEW item(s)' exclusion note appear in the markdown (COST-2 honesty)."""
    md = report.to_markdown()
    assert "excludes 1 unquantified REVIEW item(s)" in md
    assert "normalized from annual plan" in md


def test_extra_field_forbidden():
    """Fail-loud discipline (mirrors dossier.py): every model forbids extra fields."""
    with pytest.raises(ValueError, match="[Ee]xtra"):
        Money.model_validate({"framing": "EST", "usd_month": 1.0, "surprise": "x"})
    with pytest.raises(ValueError, match="[Ee]xtra"):
        StackAuditReport.model_validate(
            {**yaml.safe_load(GOLDEN_YAML.read_text(encoding="utf-8")), "surprise": "x"}
        )
