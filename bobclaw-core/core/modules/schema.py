"""Pydantic v2 models for module manifest validation."""

from typing import List
from pydantic import BaseModel, Field, field_validator, model_validator


class FaceRef(BaseModel):
    ref: str = Field(min_length=1)
    io_contract: str = Field(min_length=1)
    output_schema: str = Field(default="raw", min_length=1)

    @field_validator("io_contract")
    @classmethod
    def validate_io_contract(cls, v: str) -> str:
        valid = {"text", "tools_native", "tools_harness"}
        if v not in valid:
            raise ValueError(
                f"io_contract must be one of {valid}, got '{v}'"
            )
        return v


class ToolRef(BaseModel):
    id: str = Field(min_length=1)
    tier: str = Field(min_length=1)

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, v: str) -> str:
        valid_tiers = {"RO", "Write-Local", "Social", "Full-Access"}
        if v not in valid_tiers:
            raise ValueError(
                f"tier must be one of {valid_tiers}, got '{v}'"
            )
        return v


class ModuleManifest(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    first_user: str = Field(min_length=1)
    wow_flow: str = Field(min_length=1)
    faces: List[FaceRef] = Field(default_factory=list)
    tools: List[ToolRef] = Field(default_factory=list)
    pipes: List[str] = Field(default_factory=list)
    lks_namespace: str = Field(min_length=1)
    verification_class: str = Field(min_length=1)
    cost_posture: str = Field(min_length=1)
    always_human: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    watch_subscriptions: List[str] = Field(default_factory=list)

    # -- field validators for audience (normalise list to str) --
    @field_validator("audience", mode="before")
    @classmethod
    def coerce_audience(cls, v):
        if isinstance(v, list):
            # join list elements into a string (e.g. ["non-developers"] -> "non-developers")
            return ", ".join(str(item) for item in v)
        return v

    # -- pipes validator --
    @field_validator("pipes")
    @classmethod
    def validate_pipes(cls, v: List[str]) -> List[str]:
        valid_pipes = {"chat", "research", "build", "council"}
        for pipe in v:
            if pipe not in valid_pipes:
                raise ValueError(
                    f"pipes must contain only values from {valid_pipes}, got '{pipe}'"
                )
        return v

    # -- verification_class validator --
    @field_validator("verification_class")
    @classmethod
    def validate_verification_class(cls, v: str) -> str:
        valid = {"claim-ratchet", "spec-conformance", "critique", "human-taste"}
        if v not in valid:
            raise ValueError(
                f"verification_class must be one of {valid}, got '{v}'"
            )
        return v

    # -- model-level validator for always_human requirement (R5) --
    @model_validator(mode="after")
    def check_always_human_for_non_ro_tools(self) -> "ModuleManifest":
        non_ro_tiers = {"Write-Local", "Social", "Full-Access"}
        for tool in self.tools:
            if tool.tier in non_ro_tiers:
                if not self.always_human:
                    raise ValueError(
                        "always_human must be non-empty when any tool has tier "
                        f"in {non_ro_tiers}, but it is empty"
                    )
                break  # only need to check once
        return self
