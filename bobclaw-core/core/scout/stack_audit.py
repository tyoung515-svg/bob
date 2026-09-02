from __future__ import annotations

import datetime
import enum
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.config import KNOWN_BACKENDS


SCHEMA_VERSION = 0


class Recommendation(str, enum.Enum):
    KEEP = "keep"
    DOWNGRADE = "downgrade"
    CANCEL = "cancel"
    REVIEW = "review"


class BillingPeriod(str, enum.Enum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framing: Literal["EST"] = "EST"
    usd_month: float = 0.0
    known: bool = True
    note: str = ""

    @field_validator("framing")
    @classmethod
    def _must_be_est(cls, v: str) -> str:
        if v != "EST":
            raise ValueError(f"framing must be 'EST' (COST-2 requirement), got {v!r}")
        return v

    @field_validator("usd_month")
    @classmethod
    def _non_negative_and_rounded(cls, v: float) -> float:
        if v < 0:
            raise ValueError("usd_month must be >= 0")
        return round(v, 2)

    @property
    def badge(self) -> str:
        if not self.known:
            return "unknown (EST)"
        return f"~${self.usd_month:,.2f}/mo (EST)"

    @classmethod
    def est(cls, usd: float) -> "Money":
        return cls(usd_month=usd, known=True)

    @classmethod
    def unknown(cls, note: str = "") -> "Money":
        return cls(usd_month=0.0, known=False, note=note or "not quantified")


class BackendUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str
    calls: int = 0
    active_days: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    metered_usd: float = 0.0

    @field_validator("backend")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("backend must not be empty")
        return stripped

    @field_validator("calls", "active_days", "input_tokens", "output_tokens", "cached_tokens", "metered_usd")
    @classmethod
    def _non_negative(cls, v: int | float) -> int | float:
        if v < 0:
            raise ValueError(f"value must be >= 0, got {v}")
        return v

    @property
    def used(self) -> bool:
        return self.calls > 0

    @property
    def is_known_backend(self) -> bool:
        return self.backend in KNOWN_BACKENDS


class UsageTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 0
    window_start: str
    window_end: str
    source: str = "core.telemetry.spend"
    backends: list[BackendUsage] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError("schema_version must be 0")
        return v

    @model_validator(mode="after")
    def _validate_dates_and_backends(self) -> "UsageTelemetry":
        # Validate ISO dates
        try:
            start = datetime.date.fromisoformat(self.window_start)
            end = datetime.date.fromisoformat(self.window_end)
        except ValueError:
            raise ValueError("window_* must be ISO date YYYY-MM-DD")
        if end < start:
            raise ValueError("window_end must be >= window_start")
        # Unique backends
        seen = set()
        dups = []
        for bu in self.backends:
            if bu.backend in seen:
                dups.append(bu.backend)
            seen.add(bu.backend)
        if dups:
            raise ValueError(f"duplicate backend rows: {sorted(set(dups))}")
        return self

    @property
    def window_days(self) -> int:
        start = datetime.date.fromisoformat(self.window_start)
        end = datetime.date.fromisoformat(self.window_end)
        return max((end - start).days + 1, 1)

    def get(self, backend: str) -> Optional[BackendUsage]:
        for bu in self.backends:
            if bu.backend == backend:
                return bu
        return None

    @classmethod
    def from_spend_by_backend(
        cls,
        by_backend: dict[str, float],
        *,
        window_start: str,
        window_end: str,
        calls_by_backend: Optional[dict[str, int]] = None,
        source: str = "core.telemetry.spend",
    ) -> "UsageTelemetry":
        items = []
        for backend, usd in sorted(by_backend.items()):
            calls = calls_by_backend.get(backend, 0) if calls_by_backend else 0
            items.append(BackendUsage(backend=backend, calls=calls, metered_usd=usd))
        return cls(
            window_start=window_start,
            window_end=window_end,
            source=source,
            backends=items,
        )

    # Round-trip helpers (mirror dossier.py)
    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageTelemetry":
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml_str(cls, yaml_str: str) -> "UsageTelemetry":
        data = yaml.safe_load(yaml_str)
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: Path) -> "UsageTelemetry":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_yaml_str(f.read())


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    provider: str
    plan: str
    monthly_usd: float
    billing: BillingPeriod = BillingPeriod.MONTHLY
    annual_usd: Optional[float] = None
    backends: list[str] = Field(default_factory=list)
    downgrade_to_usd: Optional[float] = None
    notes: str = ""

    @field_validator("id", "provider", "plan")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("monthly_usd", "annual_usd")
    @classmethod
    def _non_negative_float(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("backends")
    @classmethod
    def _unique_backends(cls, v: list[str]) -> list[str]:
        seen = set()
        result = []
        for b in v:
            stripped = b.strip()
            if not stripped:
                raise ValueError("backend entry must not be empty")
            if stripped in seen:
                raise ValueError(f"duplicate backend entry: {stripped}")
            seen.add(stripped)
            result.append(stripped)
        return result

    @field_validator("downgrade_to_usd")
    @classmethod
    def _valid_downgrade(cls, v: float | None, info: Any) -> float | None:
        if v is not None:
            if v < 0:
                raise ValueError("downgrade_to_usd must be >= 0")
                # cross-field (< monthly_usd) is enforced in the model_validator below
        return v

    @model_validator(mode="after")
    def _validate_downgrade_price(self) -> "Subscription":
        if self.downgrade_to_usd is not None and self.downgrade_to_usd >= self.monthly_usd:
            raise ValueError("downgrade_to_usd must be >= 0 and cheaper than monthly_usd")
        return self


class SubscriptionsDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 0
    subscriptions: list[Subscription] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError("schema_version must be 0")
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> "SubscriptionsDoc":
        seen = set()
        dups = []
        for sub in self.subscriptions:
            if sub.id in seen:
                dups.append(sub.id)
            seen.add(sub.id)
        if dups:
            raise ValueError(f"duplicate subscription ids: {sorted(set(dups))}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubscriptionsDoc":
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml_str(cls, yaml_str: str) -> "SubscriptionsDoc":
        data = yaml.safe_load(yaml_str)
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: Path) -> "SubscriptionsDoc":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_yaml_str(f.read())


class UsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: int
    active_days: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    metered_cost: Money
    covered_backends: list[str]
    missing_backends: list[str]


class SubscriptionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: str
    provider: str
    plan: str
    monthly_cost: Money
    recommendation: Recommendation
    est_savings: Money
    usage: UsageSummary
    rationale: str


class UnmappedBackend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str
    calls: int
    metered_cost: Money
    note: str = ""


class StackAuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 0
    window_start: str
    window_end: str
    window_days: int
    generated_at: Optional[str] = None
    telemetry_source: str = "core.telemetry.spend"
    findings: list[SubscriptionFinding] = Field(default_factory=list)
    total_monthly_cost: Money
    total_est_savings: Money
    unmapped_backends: list[UnmappedBackend] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError("schema_version must be 0")
        return v

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StackAuditReport":
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml_str(cls, yaml_str: str) -> "StackAuditReport":
        data = yaml.safe_load(yaml_str)
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: Path) -> "StackAuditReport":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_yaml_str(f.read())

    def to_markdown(self) -> str:
        lines = []
        lines.append(f"# Stack audit — {self.window_start} .. {self.window_end} ({self.window_days}d)")
        lines.append("")
        lines.append(f"_Usage measured from BoB routing/cost telemetry ({self.telemetry_source}). All figures EST._")
        lines.append("")
        savings_line = f"**Total subscriptions:** {self.total_monthly_cost.badge} · **Est. savings:** {self.total_est_savings.badge}"
        if self.total_est_savings.note:
            # surface the exclusion disclosure (e.g. "excludes N unquantified REVIEW item(s)")
            # so the rendered total never silently hides an un-estimable figure (COST-2).
            savings_line += f" — {self.total_est_savings.note}"
        lines.append(savings_line)
        for finding in self.findings:
            lines.append("")
            lines.append(f"## {finding.provider} — {finding.plan}  [{finding.recommendation.value.upper()}]")
            cost_line = f"- Cost: {finding.monthly_cost.badge}"
            if finding.monthly_cost.note:
                # e.g. "normalized from annual plan" — keep the annual→monthly context visible.
                cost_line += f" ({finding.monthly_cost.note})"
            lines.append(cost_line)
            lines.append(f"- Est. savings: {finding.est_savings.badge}")
            lines.append(f"- Usage: {finding.usage.calls} calls · {finding.usage.active_days} active days · metered {finding.usage.metered_cost.badge}")
            lines.append(f"- {finding.rationale}")
        if self.unmapped_backends:
            lines.append("")
            lines.append("## Unmapped usage (no subscription covers these)")
            for u in self.unmapped_backends:
                lines.append(f"- **{u.backend}**: {u.calls} calls · metered {u.metered_cost.badge} — {u.note}")
        if self.notes:
            lines.append("")
            lines.append("## Notes")
            for n in self.notes:
                lines.append(f"- {n}")
        lines.append("")  # trailing newline
        return "\n".join(lines)


class AuditThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unused_max_calls: int = 0
    low_usage_max_calls: int = 10
    low_usage_max_active_days: int = 3

    @field_validator("unused_max_calls", "low_usage_max_calls", "low_usage_max_active_days")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @model_validator(mode="after")
    def _validate_order(self) -> "AuditThresholds":
        if self.low_usage_max_calls < self.unused_max_calls:
            raise ValueError("low_usage_max_calls must be >= unused_max_calls")
        return self


def audit_stack(
    telemetry: UsageTelemetry,
    subscriptions: SubscriptionsDoc,
    *,
    thresholds: Optional[AuditThresholds] = None,
    generated_at: Optional[str] = None,
) -> StackAuditReport:
    thresholds = thresholds or AuditThresholds()
    findings: list[SubscriptionFinding] = []
    window_str = f"{telemetry.window_start}..{telemetry.window_end}"

    # Collect all backend names in subscriptions
    all_sub_backends = set()
    for sub in subscriptions.subscriptions:
        for b in sub.backends:
            all_sub_backends.add(b)

    # Build findings per subscription
    for sub in sorted(subscriptions.subscriptions, key=lambda s: s.id):
        covered = sorted([b for b in sub.backends if telemetry.get(b) is not None])
        missing = sorted([b for b in sub.backends if telemetry.get(b) is None])
        rows = [telemetry.get(b) for b in covered if telemetry.get(b) is not None]
        calls = sum(r.calls for r in rows) if rows else 0
        active_days = max((r.active_days for r in rows), default=0)
        in_tok = sum(r.input_tokens for r in rows)
        out_tok = sum(r.output_tokens for r in rows)
        cached_tok = sum(r.cached_tokens for r in rows)
        metered = sum(r.metered_usd for r in rows)

        # Monthly cost with note if annual
        note = ""
        if sub.billing == BillingPeriod.ANNUAL:
            note = "normalized from annual plan"
        monthly_cost = Money.est(sub.monthly_usd) if not note else Money(usd_month=sub.monthly_usd, known=True, note=note)

        # Decision logic
        if not sub.backends:
            rec = Recommendation.REVIEW
            savings = Money.unknown("BoB routes no backend for this subscription")
            rationale = (
                "BoB does not route any backend for this subscription — routing telemetry "
                "cannot measure it; assess manually."
            )
        elif calls <= thresholds.unused_max_calls:
            rec = Recommendation.CANCEL
            savings = Money.est(sub.monthly_usd)
            rationale = (
                f"No BoB-routed calls to {', '.join(sub.backends)} over the {window_str} window "
                f"— appears unused (verify you don't use it outside BoB)."
            )
        elif calls <= thresholds.low_usage_max_calls and active_days <= thresholds.low_usage_max_active_days:
            if sub.downgrade_to_usd is not None:
                rec = Recommendation.DOWNGRADE
                savings = Money.est(sub.monthly_usd - sub.downgrade_to_usd)
                rationale = (
                    f"Light usage ({calls} calls / {active_days} active days) — a cheaper "
                    f"~${sub.downgrade_to_usd:,.2f}/mo tier (EST) likely suffices."
                )
            else:
                rec = Recommendation.REVIEW
                savings = Money.unknown("no cheaper-tier price provided")
                rationale = (
                    f"Light usage ({calls} calls / {active_days} active days) — consider "
                    f"downgrading; no cheaper-tier price provided, savings unquantified."
                )
        else:
            rec = Recommendation.KEEP
            savings = Money.est(0.0)
            rationale = (
                f"Active usage ({calls} calls / {active_days} active days) — keep."
            )

        usage = UsageSummary(
            calls=calls,
            active_days=active_days,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            metered_cost=Money.est(metered),
            covered_backends=covered,
            missing_backends=missing,
        )

        finding = SubscriptionFinding(
            subscription_id=sub.id,
            provider=sub.provider,
            plan=sub.plan,
            monthly_cost=monthly_cost,
            recommendation=rec,
            est_savings=savings,
            usage=usage,
            rationale=rationale,
        )
        findings.append(finding)

    # Total monthly cost
    total_monthly = Money.est(sum(sub.monthly_usd for sub in subscriptions.subscriptions))

    # Total est savings (known only)
    known_savings_sum = sum(
        f.est_savings.usd_month for f in findings if f.est_savings.known
    )
    unknown_count = sum(1 for f in findings if not f.est_savings.known)
    total_est_savings = Money.est(known_savings_sum)
    if unknown_count > 0:
        total_est_savings.note = f"excludes {unknown_count} unquantified REVIEW item(s)"

    # Unmapped backends
    unmapped: list[UnmappedBackend] = []
    for bu in telemetry.backends:
        if bu.calls > 0 and bu.backend not in all_sub_backends:
            note = (
                "local — no metered cost"
                if bu.metered_usd == 0 and bu.backend == "local"
                else "PAYG / not covered by a subscription"
            )
            unmapped.append(
                UnmappedBackend(
                    backend=bu.backend,
                    calls=bu.calls,
                    metered_cost=Money.est(bu.metered_usd),
                    note=note,
                )
            )
    unmapped.sort(key=lambda u: u.backend)

    # Notes
    notes: list[str] = []
    if any(not sub.backends for sub in subscriptions.subscriptions):
        notes.append("1+ subscription(s) not measurable via BoB routing telemetry (routed elsewhere).")
    unknown_backends = set()
    for sub in subscriptions.subscriptions:
        for b in sub.backends:
            if b not in KNOWN_BACKENDS:
                unknown_backends.add(b)
    if unknown_backends:
        notes.append(f"unknown backend(s) referenced: {sorted(unknown_backends)}")

    report = StackAuditReport(
        window_start=telemetry.window_start,
        window_end=telemetry.window_end,
        window_days=telemetry.window_days,
        telemetry_source=telemetry.source,
        findings=findings,
        total_monthly_cost=total_monthly,
        total_est_savings=total_est_savings,
        unmapped_backends=unmapped,
        notes=notes,
        generated_at=generated_at,
    )
    return report


def load_usage_telemetry(path: Path) -> UsageTelemetry:
    return UsageTelemetry.from_yaml(path)


def load_subscriptions(path: Path) -> SubscriptionsDoc:
    return SubscriptionsDoc.from_yaml(path)


def dump_report(report: StackAuditReport, path: Path) -> None:
    path.write_text(report.to_yaml(), encoding="utf-8")
