from __future__ import annotations
import pytest
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from core.config import KNOWN_BACKENDS
from core.scout.stack_audit import (
    Money,
    Recommendation,
    BillingPeriod,
    BackendUsage,
    UsageTelemetry,
    Subscription,
    SubscriptionsDoc,
    AuditThresholds,
    UsageSummary,
    SubscriptionFinding,
    UnmappedBackend,
    StackAuditReport,
    audit_stack,
    SCHEMA_VERSION,
    load_usage_telemetry,
    load_subscriptions,
    dump_report,
)
import yaml


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #

class TestMoney:
    def test_badge_known(self):
        m = Money.est(200.0)
        assert m.badge == "~$200.00/mo (EST)"

    def test_badge_unknown(self):
        m = Money.unknown()
        assert m.badge == "unknown (EST)"

    def test_badge_framing_always_est(self):
        m = Money.est(42.0)
        assert m.badge.endswith("(EST)")

    def test_framing_must_be_est(self):
        with pytest.raises(ValueError, match="EST"):
            Money(framing="GUESS", usd_month=10.0)

    def test_negative_usd_month_raises(self):
        with pytest.raises(ValueError, match="usd_month must be >= 0"):
            Money.est(-1.0)

    def test_usd_month_rounded_to_2dp(self):
        m = Money.est(1.239)
        assert m.usd_month == 1.24
        m2 = Money.est(1.005)
        assert m2.usd_month == 1.0  # banker's rounding

    def test_convenience_constructors(self):
        m = Money.est(100.0)
        assert m.usd_month == 100.0
        assert m.known is True
        m2 = Money.unknown(note="missing data")
        assert m2.known is False
        assert m2.usd_month == 0.0
        assert m2.note == "missing data"
        m3 = Money.unknown()
        assert m3.note == "not quantified"


# --------------------------------------------------------------------------- #
# UsageTelemetry validators
# --------------------------------------------------------------------------- #

class TestUsageTelemetryValidators:
    def test_schema_version_nonzero_raises(self):
        with pytest.raises(ValueError, match="schema_version"):
            UsageTelemetry(schema_version=1, window_start="2026-07-01", window_end="2026-07-07")

    def test_bad_iso_window_date_raises(self):
        with pytest.raises(ValueError, match="ISO date"):
            UsageTelemetry(schema_version=0, window_start="2026-13-01", window_end="2026-07-07")

    def test_window_end_before_start_raises(self):
        with pytest.raises(ValueError, match="window_end must be >= window_start"):
            UsageTelemetry(schema_version=0, window_start="2026-07-07", window_end="2026-07-01")

    def test_duplicate_backend_rows_raises(self):
        with pytest.raises(ValueError, match="duplicate backend"):
            UsageTelemetry(
                schema_version=0,
                window_start="2026-07-01",
                window_end="2026-07-07",
                backends=[
                    BackendUsage(backend="claude", calls=5),
                    BackendUsage(backend="claude", calls=3),
                ],
            )

    def test_window_days_inclusive(self):
        t = UsageTelemetry(schema_version=0, window_start="2026-07-01", window_end="2026-07-07")
        assert t.window_days == 7

    def test_window_days_single_day(self):
        t = UsageTelemetry(schema_version=0, window_start="2026-07-01", window_end="2026-07-01")
        assert t.window_days == 1

    def test_get_returns_row_or_none(self):
        t = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="claude", calls=5)],
        )
        assert t.get("claude").calls == 5
        assert t.get("gpt4") is None

    def test_backend_validator_non_empty(self):
        with pytest.raises(ValueError, match="backend"):
            BackendUsage(backend="  ", calls=0)


# --------------------------------------------------------------------------- #
# UsageTelemetry.from_spend_by_backend
# --------------------------------------------------------------------------- #

class TestUsageTelemetryFromSpendByBackend:
    def test_basic_build(self):
        t = UsageTelemetry.from_spend_by_backend(
            {"claude": 10.0, "gpt4": 20.0},
            window_start="2026-07-01",
            window_end="2026-07-07",
        )
        assert t.schema_version == 0
        assert t.source == "core.telemetry.spend"
        assert len(t.backends) == 2
        # should be sorted by backend
        assert t.backends[0].backend == "claude"
        assert t.backends[0].metered_usd == 10.0
        assert t.backends[0].calls == 0  # default
        assert t.backends[1].backend == "gpt4"
        assert t.backends[1].metered_usd == 20.0

    def test_with_calls_by_backend(self):
        t = UsageTelemetry.from_spend_by_backend(
            {"claude": 10.0},
            window_start="2026-07-01",
            window_end="2026-07-07",
            calls_by_backend={"claude": 5},
        )
        assert t.backends[0].calls == 5

    def test_with_extra_backend_not_in_calls(self):
        t = UsageTelemetry.from_spend_by_backend(
            {"claude": 10.0, "local": 0.0},
            window_start="2026-07-01",
            window_end="2026-07-07",
            calls_by_backend={"claude": 3},
        )
        assert t.backends[0].calls == 3
        assert t.backends[1].calls == 0  # default for local


# --------------------------------------------------------------------------- #
# Subscription validators
# --------------------------------------------------------------------------- #

class TestSubscriptionValidators:
    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="id"):
            Subscription(id="  ", provider="P", plan="PL", monthly_usd=10.0)

    def test_empty_provider_raises(self):
        with pytest.raises(ValueError, match="provider"):
            Subscription(id="s1", provider="", plan="PL", monthly_usd=10.0)

    def test_empty_plan_raises(self):
        with pytest.raises(ValueError, match="plan"):
            Subscription(id="s1", provider="P", plan=" ", monthly_usd=10.0)

    def test_negative_monthly_usd_raises(self):
        with pytest.raises(ValueError, match="monthly_usd"):
            Subscription(id="s1", provider="P", plan="PL", monthly_usd=-1.0)

    def test_downgrade_to_usd_gte_monthly_raises(self):
        with pytest.raises(ValueError, match="downgrade_to_usd"):
            Subscription(
                id="s1", provider="P", plan="PL", monthly_usd=10.0,
                downgrade_to_usd=15.0,
            )

    def test_downgrade_to_usd_equal_monthly_raises(self):
        with pytest.raises(ValueError, match="downgrade_to_usd"):
            Subscription(
                id="s1", provider="P", plan="PL", monthly_usd=10.0,
                downgrade_to_usd=10.0,
            )

    def test_duplicate_backends_raises(self):
        with pytest.raises(ValueError, match="duplicate backend"):
            Subscription(
                id="s1", provider="P", plan="PL", monthly_usd=10.0,
                backends=["claude", "claude"],
            )

    def test_backend_empty_after_strip_raises(self):
        with pytest.raises(ValueError, match="backend"):
            Subscription(
                id="s1", provider="P", plan="PL", monthly_usd=10.0,
                backends=["  ", "gpt4"],
            )


# --------------------------------------------------------------------------- #
# SubscriptionsDoc validators
# --------------------------------------------------------------------------- #

class TestSubscriptionsDocValidators:
    def test_duplicate_subscription_ids_raises(self):
        with pytest.raises(ValueError, match="duplicate subscription id"):
            SubscriptionsDoc(
                subscriptions=[
                    Subscription(id="s1", provider="P", plan="PL", monthly_usd=10.0),
                    Subscription(id="s1", provider="Q", plan="PL2", monthly_usd=20.0),
                ]
            )

    def test_schema_version_nonzero_raises(self):
        with pytest.raises(ValueError, match="schema_version"):
            SubscriptionsDoc(
                schema_version=1,
                subscriptions=[],
            )


# --------------------------------------------------------------------------- #
# audit_stack - recommendation branches
# --------------------------------------------------------------------------- #

class TestAuditStackRecommendations:
    def make_telemetry(self, backends_data: dict[str, tuple]) -> UsageTelemetry:
        """
        backends_data: {backend: (calls, active_days, metered_usd)}
        """
        rows = []
        for b, (c, d, u) in backends_data.items():
            rows.append(BackendUsage(
                backend=b,
                calls=c,
                active_days=d,
                metered_usd=u,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
            ))
        return UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=rows,
        )

    def make_subs(self, subs: list[dict]) -> SubscriptionsDoc:
        return SubscriptionsDoc(
            subscriptions=[Subscription(**s) for s in subs]
        )

    def test_cancel_zero_calls(self):
        telemetry = self.make_telemetry({"claude": (0, 0, 0)})
        subs = self.make_subs([
            {
                "id": "s1",
                "provider": "P",
                "plan": "PL",
                "monthly_usd": 50.0,
                "backends": ["claude"],
            }
        ])
        report = audit_stack(telemetry, subs)
        assert len(report.findings) == 1
        f = report.findings[0]
        assert f.recommendation == Recommendation.CANCEL
        assert f.est_savings.known is True
        assert f.est_savings.usd_month == 50.0
        assert "No BoB-routed calls" in f.rationale

    def test_downgrade_light_usage_with_downgrade_target(self):
        telemetry = self.make_telemetry({"gpt4": (5, 2, 0.3)})
        subs = self.make_subs([
            {
                "id": "s2",
                "provider": "Q",
                "plan": "PL2",
                "monthly_usd": 100.0,
                "backends": ["gpt4"],
                "downgrade_to_usd": 60.0,
            }
        ])
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.recommendation == Recommendation.DOWNGRADE
        assert f.est_savings.known is True
        assert f.est_savings.usd_month == 40.0  # 100-60
        assert "Light usage" in f.rationale
        assert "$60.00/mo tier (EST)" in f.rationale

    def test_review_light_usage_no_downgrade(self):
        telemetry = self.make_telemetry({"gpt4": (5, 2, 0.3)})
        subs = self.make_subs([
            {
                "id": "s3",
                "provider": "R",
                "plan": "PL3",
                "monthly_usd": 80.0,
                "backends": ["gpt4"],
                # downgrade_to_usd not set
            }
        ])
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.recommendation == Recommendation.REVIEW
        assert f.est_savings.known is False
        assert "no cheaper-tier price provided" in f.rationale

    def test_review_no_backends(self):
        telemetry = self.make_telemetry({"claude": (100, 20, 5.0)})
        subs = self.make_subs([
            {
                "id": "s4",
                "provider": "S",
                "plan": "PL4",
                "monthly_usd": 200.0,
                "backends": [],  # empty
            }
        ])
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.recommendation == Recommendation.REVIEW
        assert f.est_savings.known is False
        assert "BoB does not route any backend" in f.rationale

    def test_keep_high_usage(self):
        telemetry = self.make_telemetry({"claude": (400, 20, 15.0)})
        subs = self.make_subs([
            {
                "id": "s5",
                "provider": "T",
                "plan": "PL5",
                "monthly_usd": 150.0,
                "backends": ["claude"],
            }
        ])
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.recommendation == Recommendation.KEEP
        assert f.est_savings.known is True
        assert f.est_savings.usd_month == 0.0
        assert "Active usage" in f.rationale


# --------------------------------------------------------------------------- #
# audit_stack - aggregates
# --------------------------------------------------------------------------- #

class TestAuditStackAggregates:
    def test_total_monthly_cost_sum(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="claude", calls=0, active_days=0)],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="a", provider="P", plan="PL", monthly_usd=100.0, backends=["claude"]),
                Subscription(id="b", provider="Q", plan="PL2", monthly_usd=50.0, backends=["gpt4"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        assert report.total_monthly_cost.usd_month == 150.0

    def test_total_est_savings_excludes_review_unknown(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="claude", calls=0, active_days=0),
                BackendUsage(backend="gpt4", calls=0, active_days=0),
            ],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="a", provider="P", plan="PL", monthly_usd=100.0, backends=["claude"]),
                Subscription(id="b", provider="Q", plan="PL2", monthly_usd=50.0, backends=["gpt4"]),
                Subscription(id="c", provider="R", plan="PL3", monthly_usd=30.0, backends=[]),
            ]
        )
        report = audit_stack(telemetry, subs)
        # a -> CANCEL (100), b -> CANCEL (50), c -> REVIEW unknown
        assert report.total_est_savings.usd_month == 150.0
        assert "excludes 1 unquantified REVIEW item" in report.total_est_savings.note

    def test_unmapped_backends(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="claude", calls=10, metered_usd=5.0),
                BackendUsage(backend="gpt4", calls=20, metered_usd=8.0),
                BackendUsage(backend="local", calls=1, metered_usd=0.0),
            ],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="a", provider="P", plan="PL", monthly_usd=100.0,
                            backends=["claude", "local"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        # gpt4 is unmapped (backend not in any subscription's backends)
        assert len(report.unmapped_backends) == 1
        u = report.unmapped_backends[0]
        assert u.backend == "gpt4"
        assert u.calls == 20
        assert u.metered_cost.usd_month == 8.0
        # local is covered by sub, not unmapped

    def test_findings_sorted_by_subscription_id(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="c", calls=0, active_days=0)],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="z", provider="P", plan="PL", monthly_usd=10.0, backends=["c"]),
                Subscription(id="a", provider="Q", plan="PL2", monthly_usd=20.0, backends=["c"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        ids = [f.subscription_id for f in report.findings]
        assert ids == ["a", "z"]

    def test_unmapped_backend_note_local(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="local", calls=5, metered_usd=0.0),
            ],
        )
        subs = SubscriptionsDoc(subscriptions=[])
        report = audit_stack(telemetry, subs)
        assert len(report.unmapped_backends) == 1
        u = report.unmapped_backends[0]
        assert u.backend == "local"
        assert u.note == "local — no metered cost"

    def test_unmapped_backend_payg_note(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="gpt4", calls=5, metered_usd=2.0),
            ],
        )
        subs = SubscriptionsDoc(subscriptions=[])
        report = audit_stack(telemetry, subs)
        u = report.unmapped_backends[0]
        assert u.backend == "gpt4"
        assert u.note == "PAYG / not covered by a subscription"


# --------------------------------------------------------------------------- #
# EST invariant tests
# --------------------------------------------------------------------------- #

class TestESTInvariant:
    def test_to_dict_est_invariant(self):
        # Build a report with at least one of each recommendation
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="claude", calls=0, active_days=0),
                BackendUsage(backend="gpt4", calls=5, active_days=2),
                BackendUsage(backend="local", calls=400, active_days=20),
            ],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="cancel", provider="C", plan="PL", monthly_usd=50.0,
                            backends=["claude"]),
                Subscription(id="downgrade", provider="D", plan="PL2", monthly_usd=100.0,
                            backends=["gpt4"], downgrade_to_usd=60.0),
                Subscription(id="keep", provider="K", plan="PL3", monthly_usd=200.0,
                            backends=["local"]),
                Subscription(id="review_no_downgrade", provider="R", plan="PL4", monthly_usd=30.0,
                            backends=["gpt4"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        d = report.to_dict()
        self._check_est_in_dict(d)

    def _check_est_in_dict(self, obj):
        if isinstance(obj, dict):
            if "usd_month" in obj:
                assert "framing" in obj, f"Missing framing in dict with usd_month: {obj}"
                assert obj["framing"] == "EST", f"framing not EST: {obj}"
            for value in obj.values():
                self._check_est_in_dict(value)
        elif isinstance(obj, list):
            for item in obj:
                self._check_est_in_dict(item)

    def test_to_markdown_est_invariant(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="claude", calls=0, active_days=0),
                BackendUsage(backend="gpt4", calls=5, active_days=2),
                BackendUsage(backend="local", calls=400, active_days=20),
            ],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="cancel", provider="C", plan="PL", monthly_usd=50.0,
                            backends=["claude"]),
                Subscription(id="downgrade", provider="D", plan="PL2", monthly_usd=100.0,
                            backends=["gpt4"], downgrade_to_usd=60.0),
                Subscription(id="keep", provider="K", plan="PL3", monthly_usd=200.0,
                            backends=["local"]),
                Subscription(id="review_no_downgrade", provider="R", plan="PL4", monthly_usd=30.0,
                            backends=["gpt4"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        markdown = report.to_markdown()
        lines = markdown.split("\n")
        for line in lines:
            if "$" in line:
                assert "(EST)" in line, f"Line contains $ but no (EST): {line!r}"


# --------------------------------------------------------------------------- #
# Round-trip tests
# --------------------------------------------------------------------------- #

class TestRoundTrip:
    def test_stack_audit_report_round_trip(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="claude", calls=0)],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="s1", provider="P", plan="PL", monthly_usd=50.0,
                            backends=["claude"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        d = report.to_dict()
        restored = StackAuditReport.from_dict(d)
        assert restored == report
        yaml_str = report.to_yaml()
        restored2 = StackAuditReport.from_yaml_str(yaml_str)
        assert restored2 == report

    def test_usage_telemetry_round_trip(self):
        t = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="claude", calls=5)],
        )
        d = t.to_dict()
        restored = UsageTelemetry.from_dict(d)
        assert restored == t
        yaml_str = t.to_yaml()
        restored2 = UsageTelemetry.from_yaml_str(yaml_str)
        assert restored2 == t

    def test_subscriptions_doc_round_trip(self):
        s = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="s1", provider="P", plan="PL", monthly_usd=10.0),
            ]
        )
        d = s.to_dict()
        restored = SubscriptionsDoc.from_dict(d)
        assert restored == s
        yaml_str = s.to_yaml()
        restored2 = SubscriptionsDoc.from_yaml_str(yaml_str)
        assert restored2 == s


# --------------------------------------------------------------------------- #
# Additional edge cases: annual billing note, missing backends in report
# --------------------------------------------------------------------------- #

class TestEdgeCases:
    def test_annual_billing_note(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="claude", calls=0, active_days=0)],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="s", provider="P", plan="PL", monthly_usd=120.0,
                            billing=BillingPeriod.ANNUAL, backends=["claude"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.monthly_cost.note == "normalized from annual plan"

    def test_missing_backends_in_report(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="claude", calls=5)],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="s", provider="P", plan="PL", monthly_usd=10.0,
                            backends=["gpt4"]),  # gpt4 not in telemetry
            ]
        )
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.usage.covered_backends == []
        assert f.usage.missing_backends == ["gpt4"]

    def test_unknown_backend_note_in_report(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[BackendUsage(backend="unknown_backend", calls=0)],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="s", provider="P", plan="PL", monthly_usd=10.0,
                            backends=["unknown_backend"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        assert any("unknown backend(s)" in note for note in report.notes)

    def test_calls_default_to_zero_in_from_spend(self):
        t = UsageTelemetry.from_spend_by_backend(
            {"claude": 10.0},
            window_start="2026-07-01",
            window_end="2026-07-07",
        )
        assert t.backends[0].calls == 0

    def test_usage_summary_metered_cost(self):
        telemetry = UsageTelemetry(
            schema_version=0,
            window_start="2026-07-01",
            window_end="2026-07-07",
            backends=[
                BackendUsage(backend="claude", calls=10, active_days=3, metered_usd=5.0),
                BackendUsage(backend="gpt4", calls=20, active_days=2, metered_usd=3.0),
            ],
        )
        subs = SubscriptionsDoc(
            subscriptions=[
                Subscription(id="s", provider="P", plan="PL", monthly_usd=100.0,
                            backends=["claude", "gpt4"]),
            ]
        )
        report = audit_stack(telemetry, subs)
        f = report.findings[0]
        assert f.usage.calls == 30
        assert f.usage.active_days == 3  # max
        assert f.usage.metered_cost.usd_month == 8.0
        assert f.usage.covered_backends == ["claude", "gpt4"]
        assert f.usage.missing_backends == []
