from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator


SUPPORTED_VERSION = 0
SOURCE_KINDS = frozenset({
    "changelog", "pricing", "release", "registry", "community", "rss", "docs"
})
_DEFAULT_SOURCES_PATH = Path(__file__).parent / "sources_v0.yaml"


def default_sources_path() -> Path:
    """Return the default path to the sources YAML file."""
    return _DEFAULT_SOURCES_PATH


class WatchSchedule(BaseModel):
    """Cron schedule for a watch profile."""

    cron: str
    task: str
    face_hint: str | None = None
    owner: str | None = None

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        if not croniter.is_valid(v):
            raise ValueError(f"invalid cron expression: {v!r}")
        return v

    @field_validator("task")
    @classmethod
    def _validate_task(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task must be non-empty")
        return v


class WatchSource(BaseModel):
    """Single watch source specification."""

    id: str
    name: str
    kind: str
    url: str
    provider: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    auth_required: bool = False
    fingerprint_fields: list[str] | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id must be non-empty")
        return v

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in SOURCE_KINDS:
            raise ValueError(f"kind {v!r} is not a valid source kind; allowed: {sorted(SOURCE_KINDS)}")
        return v

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not v or not v.startswith("http"):
            raise ValueError(f"url must be non-empty and must start with http: {v!r}")
        return v

    @field_validator("auth_required")
    @classmethod
    def _validate_auth_required(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "authed sources require S3 credential custody / MS8-CC1; "
                "not shippable in v0"
            )
        return v


class SourcesRegistry(BaseModel):
    """Loaded and validated sources registry."""

    version: int
    sources: list[WatchSource]
    schedule: WatchSchedule | None = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: int) -> int:
        if v != SUPPORTED_VERSION:
            raise ValueError(f"version must be {SUPPORTED_VERSION}, got {v} (unsupported)")
        return v

    @model_validator(mode="after")
    def _validate_sources(self) -> "SourcesRegistry":
        if not self.sources:
            raise ValueError("sources cannot be empty")
        seen_ids = set()
        dups = []
        for src in self.sources:
            if src.id in seen_ids:
                dups.append(src.id)
            seen_ids.add(src.id)
        if dups:
            raise ValueError(f"duplicate source ids: {dups}")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "SourcesRegistry":
        """Load and validate a sources registry from a YAML file."""
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            raise ValueError("YAML root must be a mapping")
        return cls.model_validate(raw)

    def get(self, source_id: str) -> WatchSource:
        """Look up a source by id. Raises KeyError if not found."""
        for src in self.sources:
            if src.id == source_id:
                return src
        raise KeyError(f"unknown source id: {source_id}")

    def ids(self) -> list[str]:
        """Return source ids in file order."""
        return [src.id for src in self.sources]

    def enabled_sources(self) -> list[WatchSource]:
        """Return only enabled sources."""
        return [src for src in self.sources if src.enabled]

    def schedule_profile(
        self,
        name: str,
        *,
        cron: str | None = None,
        task: str | None = None,
        face_hint: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Build a scheduler profile envelope binding watch to cron.

        Fills missing values from self.schedule if possible.
        Validates the resulting schedule via WatchSchedule.
        Returns {"name":..., "schedule":{...}}.
        """
        # Resolve defaults from self.schedule
        if cron is None:
            if self.schedule is None:
                raise ValueError("cron is required when no global schedule is set")
            cron = self.schedule.cron
        if task is None:
            if self.schedule is None:
                raise ValueError("task is required when no global schedule is set")
            task = self.schedule.task
        if face_hint is None and self.schedule is not None:
            face_hint = self.schedule.face_hint
        if owner is None and self.schedule is not None:
            owner = self.schedule.owner

        # Validate via WatchSchedule (will raise on invalid cron/task)
        try:
            WatchSchedule(cron=cron, task=task, face_hint=face_hint, owner=owner)
        except Exception as e:
            raise ValueError(f"invalid schedule profile: {e}") from e

        return {
            "name": name,
            "schedule": {
                "cron": cron,
                "task": task,
                "face_hint": face_hint,
                "owner": owner,
            },
        }


def load_sources(path: Path | None = None) -> SourcesRegistry:
    """Load the sources registry from a YAML path (or the default)."""
    if path is None:
        path = default_sources_path()
    return SourcesRegistry.from_yaml(path)
