"""Versioned deployment data, shared by core, gateway and bridge.

Unset selects configured-lane policy. An explicit selector never falls back.
No credentials, probes or generation here.
The selection is process configuration; restart after changing a manifest.
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Annotated, Literal
from types import SimpleNamespace

from pydantic import (BaseModel, ConfigDict, Field, PrivateAttr,
                      StringConstraints, field_validator, model_validator)
import yaml

# Neutral example for explicit BOB_ROSTER_FILE configuration. Unset keeps the
# provider-free configured-lane policy below; no external roster is assumed.
DEFAULT_ROSTER_FILE = Path(__file__).resolve().parents[1] / "config/roster.example.yaml"
Name = Annotated[str, StringConstraints(min_length=1)]


class RosterConfigurationError(ValueError):
    """Invalid deployment configuration; startup must stop."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _OrderedData(_Data):
    """Retain supplied field order/omission for the existing gateway JSON wire format."""
    _source_order: tuple[str, ...] = PrivateAttr(default=())

    @model_validator(mode="wrap")
    @classmethod
    def remember_order(cls, value, handler):
        model = handler(value)
        if isinstance(value, dict):
            model._source_order = tuple(value)
        return model

    def as_supplied(self) -> dict:
        validated = self.model_dump(exclude_unset=True)
        return {field: validated[field] for field in self._source_order}


class ServiceModel(_OrderedData):
    label: Name
    backend: Name
    model: Name
    transport: Name
    enabled: bool
    disabled_reason: str | None = None
    capabilities: str | None = None
    availability: str | None = None
    roles: list[Name] | None = None
    tools_verified: bool | None = None
    effort: str | None = None
    reported_model_names: list[Name] | None = None
    model_identity: Name | None = None
    family: Name | None = None


class ServiceTeam(_OrderedData):
    label: Name
    manager: Name
    worker: Name
    critic: Name
    runnable: bool
    bulk: Name | None = None
    senior: Name | None = None
    execution_mode: str | None = None
    bulk_status: str | None = None
    reason: str | None = None


class AgentServices(_Data):
    models: dict[Name, ServiceModel]
    teams: dict[Name, ServiceTeam]
    worker_choices: dict[Name, list[Name]]

    @model_validator(mode="after")
    def references(self):
        for team in self.teams.values():
            for seat in (team.manager, team.worker, team.critic, team.bulk, team.senior):
                if seat is not None and seat not in self.models:
                    raise ValueError(f"unknown Agent Services seat: {seat}")
        for team, choices in self.worker_choices.items():
            if team not in self.teams or any(seat not in self.models for seat in choices):
                raise ValueError(f"invalid worker choices for team: {team}")
        return self


class TeamRole(_Data):
    backend: Name
    escalation_chain: list[Name]


class IntentFaces(_Data):
    worker: Name
    plan_code: Name
    plan_concept: Name


class Routing(_Data):
    intent_faces: IntentFaces
    class_faces: dict[Name, Name]
    council_face: Name
    workspace_worker_face: Name
    preferred_backend: Name
    escalation_backend: Name


class ExecuteFallbacks(_Data):
    opencode_unavailable: Name
    claude_throttled: Name
    agy_throttled: Name
    codex_throttled: Name
    kimi_429: Name
    pi_429: Name
    pi_error: Name


class CouncilSeat(_Data):
    backend: Name
    fallback_chain: list[Name]


class Defaults(_Data):
    face_preferred_backend: Name
    face_escalation_backend: Name
    execute_fallbacks: ExecuteFallbacks
    gate_critic_backend: str
    critic_fallback_backend: str
    council_seat_backends: dict[Name, CouncilSeat]
    council_default_seats: list[Name] = Field(min_length=1)
    council_default_synth_posture: Name
    council_synth_backend: Name
    council_ground_backend: Name

    @model_validator(mode="after")
    def council_references(self):
        for seat in [*self.council_default_seats, self.council_default_synth_posture]:
            if seat not in self.council_seat_backends:
                raise ValueError(f"unknown council posture: {seat}")
        return self


class DeploymentRoster(_Data):
    schema_version: Literal[1]
    profiles_dir: Name
    agent_services: AgentServices
    builtin_teams: dict[Name, dict[Literal["apex", "worker", "critic"], TeamRole]]
    routing: Routing
    defaults: Defaults
    _file: Path = PrivateAttr()
    _profiles_path: Path = PrivateAttr()

    @field_validator("schema_version", mode="before")
    @classmethod
    def version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported roster schema_version (expected integer 1)")
        return value

    @property
    def file(self) -> Path:
        return self._file

    @property
    def profiles_path(self) -> Path:
        return self._profiles_path


def load_roster(path: str | Path) -> DeploymentRoster:
    """Read and validate a selected file. Relative profile paths are manifest-relative."""
    selected = Path(path).expanduser().resolve()
    try:
        roster = DeploymentRoster.model_validate(yaml.load(
            selected.read_text(encoding="utf-8"), Loader=_UniqueLoader))
        roster._file = selected
        roster._profiles_path = (selected.parent / roster.profiles_dir).resolve()
        if not roster.profiles_path.is_dir():
            raise ValueError(f"profiles directory not found: {roster.profiles_path}")
        # Check the routing references without importing core.config / the face
        # registry (config consumes these defaults at import time).
        ids = set()
        for profile in sorted([*roster.profiles_path.glob("*.yaml"),
                               *roster.profiles_path.glob("*.yml")]):
            raw = yaml.load(profile.read_text(encoding="utf-8"), Loader=_UniqueLoader)
            face_id = raw["id"]
            if not isinstance(face_id, str) or not face_id.strip() or face_id in ids:
                raise ValueError(f"invalid or duplicate face id in {profile}")
            ids.add(face_id)
        routing = roster.routing
        for face in [*routing.intent_faces.model_dump().values(),
                     *routing.class_faces.values(), routing.council_face,
                     routing.workspace_worker_face]:
            if face not in ids:
                raise ValueError(f"unknown routing face: {face}")
        return roster
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise RosterConfigurationError(
            f"BOB_ROSTER_FILE configuration error ({selected}): {exc}"
        ) from exc


@lru_cache(maxsize=None)
def _cached_roster(path: Path) -> DeploymentRoster:
    return load_roster(path)


def get_roster() -> DeploymentRoster | SimpleNamespace:
    """Effective deployment roster; never substitute for a bad explicit selector."""
    raw = os.environ.get("BOB_ROSTER_FILE")
    if raw is not None and not raw.strip():
        raise RosterConfigurationError("BOB_ROSTER_FILE configuration error: empty selector")
    if raw is None:
        return _CONFIGURED_POLICY
    selected = Path(raw).expanduser().resolve()
    return _cached_roster(selected)


# Empty auxiliary choices are intentional: features must have explicit lane
# requirements. This policy contains no deployment-specific provider names.
_CONFIGURED_POLICY = SimpleNamespace(
    file=None,
    profiles_path=Path(__file__).parent / "faces/templates",
    agent_services=AgentServices(models={}, teams={}, worker_choices={}),
    builtin_teams={},
    routing=SimpleNamespace(
        intent_faces=IntentFaces(worker="assistant", plan_code="reviewer", plan_concept="researcher"),
        class_faces={"worker": "assistant", "route_cheap": "assistant", "plan_code": "reviewer",
                     "plan_concept": "researcher", "synth_mid": "researcher", "synth_deep": "researcher"},
        council_face="", workspace_worker_face="", preferred_backend="", escalation_backend=""),
    defaults=SimpleNamespace(
        face_preferred_backend="", face_escalation_backend="",
        execute_fallbacks=SimpleNamespace(**{k: "" for k in ExecuteFallbacks.model_fields}),
        gate_critic_backend="", critic_fallback_backend="", council_seat_backends={},
        council_default_seats=[], council_default_synth_posture="", council_synth_backend="",
        council_ground_backend=""))
