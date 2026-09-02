"""BoBClaw Scout dossier format (module M1 / MS8-SC1).

A *dossier* is the evidence bundle behind one Scout finding (e.g. "DeepSeek V4
Flash price cut"). It carries CLAIMS; each claim is backed by EVIDENCE REFS, an
EST cost-impact, and a maturity tag. Scout's verification class is
``claim-ratchet`` (MODULES.md M1) — declared and recorded here, but NOT executed
here: this module is data + validation only, fixture-driven, no network (inv. 11).

COST-2 discipline: every cost figure is badged **EST** — no fake precision — and
the EST badge MUST survive serialize->load round-trips (``CostImpact.framing`` is a
``Literal["EST"]`` that re-validates on load). Fail-loud author-time validation
mirrors the faces / watch registry discipline (every model forbids extra fields).

Round-trip contract (SC2 consumes this): for any valid ``Dossier`` d ::

    Dossier.from_dict(d.to_dict()) == d
    Dossier.from_yaml_str(d.to_yaml()) == d
    d.to_dict()["claims"][0]["cost_impact"]["framing"] == "EST"
"""
from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Str-mixin enums (plain strings when serialised)
# ---------------------------------------------------------------------------

class Maturity(str, enum.Enum):
    GA = "ga"
    PREVIEW = "preview"
    BETA = "beta"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"
    RUMORED = "rumored"


class CostDirection(str, enum.Enum):
    CHEAPER = "cheaper"
    PRICIER = "pricier"
    NEW_COST = "new_cost"
    NO_CHANGE = "no_change"
    UNKNOWN = "unknown"


class VerificationStatus(str, enum.Enum):
    UNVERIFIED = "unverified"
    ENTAILED = "entailed"
    CORROBORATED = "corroborated"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"


class Category(str, enum.Enum):
    PRICING = "pricing"
    MODEL_RELEASE = "model_release"
    CAPABILITY = "capability"
    TOOL = "tool"
    LOCAL_RUNTIME = "local_runtime"
    BENCHMARK = "benchmark"
    DEPRECATION = "deprecation"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    url: str
    excerpt: str
    retrieved_at: Optional[str] = None

    @field_validator("source_id")
    @classmethod
    def source_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_id must be non-empty")
        return v.strip()

    @field_validator("url")
    @classmethod
    def url_valid(cls, v: str) -> str:
        # Evidence citations require a real http(s) scheme (case-insensitive). Stricter
        # than the watch source-registry's lenient startswith("http") on purpose: an
        # evidence ref must point at a fetchable source, so a typo'd scheme (httpss://)
        # or bare "http" is rejected here. URLs are never fetched in v0 (inv. 11).
        if not v or not v.lower().startswith(("http://", "https://")):
            raise ValueError(f"url must be non-empty and start with http: {v!r}")
        return v

    @field_validator("excerpt")
    @classmethod
    def excerpt_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("excerpt must be non-empty")
        return v.strip()


class CostImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framing: Literal["EST"] = "EST"
    direction: CostDirection
    magnitude_usd_month: Optional[float] = None
    unit: Optional[str] = None
    note: str = ""

    @field_validator("framing")
    @classmethod
    def framing_must_be_est(cls, v: str) -> str:
        if v != "EST":
            raise ValueError(
                "cost impact framing must be 'EST' (COST-2: no unbadged figures)"
            )
        return v

    @field_validator("magnitude_usd_month")
    @classmethod
    def magnitude_non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("magnitude_usd_month must be >= 0")
        return v

    @model_validator(mode="after")
    def directional_requires_info(self) -> "CostImpact":
        if self.direction in {
            CostDirection.CHEAPER,
            CostDirection.PRICIER,
            CostDirection.NEW_COST,
        }:
            if self.magnitude_usd_month is None and not self.note.strip():
                raise ValueError(
                    "a directional cost impact needs a magnitude or a note "
                    "(COST-2: no bare claims)"
                )
        return self

    @property
    def badge(self) -> str:
        """Human string that ALWAYS ends with ``(EST)`` — never a bare figure."""
        dir_word_map = {
            CostDirection.CHEAPER: "cheaper",
            CostDirection.PRICIER: "pricier",
            CostDirection.NEW_COST: "new cost",
        }
        if self.direction == CostDirection.NO_CHANGE:
            return "no cost change (EST)"
        if self.direction == CostDirection.UNKNOWN:
            return "cost impact unknown (EST)"
        word = dir_word_map[self.direction]
        if self.magnitude_usd_month is not None:
            mag_str = f"~${self.magnitude_usd_month:g}{self.unit or '/mo'}"
            return f"{mag_str} {word} (EST)"
        return f"{word} (EST)"


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vclass: Literal["claim-ratchet"] = "claim-ratchet"
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    votes: Optional[int] = None
    note: str = ""


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    statement: str
    evidence: list[EvidenceRef]
    cost_impact: CostImpact
    maturity: Maturity
    verification: Verification = Field(default_factory=Verification)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id must be non-empty")
        return v.strip()

    @field_validator("statement")
    @classmethod
    def statement_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("statement must be non-empty")
        return v.strip()

    @model_validator(mode="after")
    def evidence_non_empty(self) -> "Claim":
        if not self.evidence:
            raise ValueError(
                f"claim {self.id} has no evidence "
                "(a dossier claim must cite at least one source)"
            )
        return self


class Dossier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 0
    id: str
    title: str
    summary: str = ""
    category: Category
    sources_ref: str = "scout/sources_v0.yaml"
    claims: list[Claim]
    created_at: Optional[str] = None

    @field_validator("schema_version")
    @classmethod
    def schema_version_must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError(f"schema_version must be 0, got {v} (unsupported)")
        return v

    @field_validator("id")
    @classmethod
    def id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id must be non-empty")
        return v.strip()

    @field_validator("title")
    @classmethod
    def title_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must be non-empty")
        return v.strip()

    @model_validator(mode="after")
    def validate_claims(self) -> "Dossier":
        if not self.claims:
            raise ValueError("dossier must have at least one claim")
        ids = [c.id for c in self.claims]
        duplicate_ids = [cid for cid in ids if ids.count(cid) > 1]
        if duplicate_ids:
            raise ValueError(f"duplicate claim ids: {sorted(set(duplicate_ids))}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict) -> "Dossier":
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml_str(cls, text: str) -> "Dossier":
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError("dossier YAML root must be a mapping")
        return cls.model_validate(raw)

    @classmethod
    def from_yaml(cls, path: Path) -> "Dossier":
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        return cls.from_yaml_str(text)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 0


def load_dossier(path: Path) -> Dossier:
    """Load and validate a dossier from a YAML path."""
    return Dossier.from_yaml(Path(path))


def dump_dossier(dossier: Dossier, path: Path) -> None:
    """Serialise a dossier to a YAML path (utf-8)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(dossier.to_yaml())
