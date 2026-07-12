"""
BoBClaw Core — Face / Role registry

Loads all YAML profiles from core/faces/profiles/ and exposes them
through a typed Pydantic model with fast dict-based lookups.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# Default profiles directory relative to this file
_DEFAULT_PROFILES_DIR = Path(__file__).parent / "profiles"

# Disallowed abstract labels that never match a registered native or MCP tool ID.
# Profiles must use real tool IDs (or an honest empty list) in allowed_tools.
FANTASY_TOOL_LABELS: frozenset[str] = frozenset(
    {"code", "files", "shell", "search", "docs", "email", "browser"}
)


# ─── Pydantic model ───────────────────────────────────────────────────────────

class Face(BaseModel):
    id: str
    name: str
    avatar: str = "🤖"
    system_prompt: str
    preferred_backend: str = "local"
    allowed_tools: list[str] = Field(default_factory=list)
    escalation_backend: str = "claude_api"
    ui_theme: str = "grey"
    # ── Display metadata (U2, Decision D10) ──
    # Friendly, human-readable presentation fields consumed by UI surfaces (the
    # G1 ``/capabilities`` payload, the Simple/Pro mode picker). ALL optional and
    # DISPLAY-ONLY: they never enter prompt assembly (the face's only prompt
    # contribution is ``system_prompt``). Absent ⇒ a consumer falls back to ``id``.
    #   display_name — a normie-facing name (no vendor/backend jargon).
    #   blurb        — a one-line description of what the face does.
    #   simple_slot  — plain-language Simple-mode slot (e.g. quick | think_hard |
    #                  team_of_experts) driving §6's mode picker with NO hardcoded
    #                  app-side map; None ⇒ the face is not surfaced in Simple mode.
    display_name: Optional[str] = None
    blurb: Optional[str] = None
    simple_slot: Optional[str] = None
    # ── JOAT v0: role/tier dimension (apex|worker|critic) ──
    # Orthogonal to which face answers (_select_face is unchanged). Optional so
    # every existing profile still loads. The `teams.resolve` layer maps
    # (role, context) → backend; with NO active team it ignores role entirely and
    # returns this face's preferred_backend (byte-for-byte today's answer).
    # Literal gives membership validation for free (pydantic rejects other values).
    role: Optional[Literal["apex", "worker", "critic"]] = None
    # ── Claude Code posture (C2) ──
    # CLI-flag policy fed to the ``claude_code`` backend per face. Translated to
    # argv by ``ClaudeCodeClient._posture_flags`` (e.g. permission_mode,
    # allowed_tools, scratch_dir). Empty for non-CC faces.
    cc_posture: dict = Field(default_factory=dict)
    # ── Antigravity (agy) posture ──
    # Policy fed to the ``agy_code`` backend per face: ``model``, ``mode``
    # (``scratch_write`` ⇒ read the repo), ``add_dirs``, ``allow_tools``. Empty
    # for non-agy faces.
    agy_posture: dict = Field(default_factory=dict)
    # ── Codex (codex_code) posture ──
    # Policy fed to the ``codex_code`` backend per face: ``profile`` (glm|deepseek|
    # qwen) or ``model`` (a litellm model name), ``mode`` (``scratch_write`` ⇒ read
    # the repo), ``brief``, ``add_dirs``. Empty for non-codex faces. The fan-out
    # threads only ``model`` (via dispatch→worker model_override); the planner tier
    # reads the full posture. Empty for non-codex faces.
    codex_posture: dict = Field(default_factory=dict)
    # ── Critic gate (handoff 008, LKS v3.1 rule 16) ──
    critic_backend: Optional[str] = None
    critic_prompt_template: Optional[str] = None

    @field_validator("system_prompt")
    @classmethod
    def system_prompt_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("system_prompt must not be empty")
        return stripped

    @field_validator("id")
    @classmethod
    def id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id must not be empty")
        return v.strip()

    @field_validator("allowed_tools")
    @classmethod
    def allowed_tools_no_fantasy_labels(cls, v: list[str]) -> list[str]:
        """Reject abstract capability labels that do not gate real tools."""
        offenders = [t for t in v if t in FANTASY_TOOL_LABELS]
        if offenders:
            raise ValueError(
                f"allowed_tools contains fantasy labels with no registered tool: "
                f"{', '.join(offenders)}"
            )
        return v

    @field_validator("critic_backend")
    @classmethod
    def critic_backend_known(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            from core.config import MAX_WORKER_USD_BY_BACKEND
            if v not in MAX_WORKER_USD_BY_BACKEND:
                raise ValueError(f"critic_backend {v!r} is not a known backend")
        return v

    @field_validator("critic_prompt_template")
    @classmethod
    def critic_prompt_has_placeholders(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            if "{subtask_text}" not in v or "{worker_output}" not in v:
                raise ValueError("critic_prompt_template must contain {subtask_text} and {worker_output}")
        return v


class FaceSummary(BaseModel):
    id: str
    name: str
    avatar: str
    preferred_backend: str
    ui_theme: str
    # Display metadata (U2/D10) — carried onto the compact summary so it reaches
    # ``/api/faces`` and, in turn, the gateway ``/capabilities`` payload. Optional;
    # null when the face does not populate them.
    display_name: Optional[str] = None
    blurb: Optional[str] = None
    simple_slot: Optional[str] = None


# ─── Registry ─────────────────────────────────────────────────────────────────

class FaceRegistry:
    """Loads and indexes Face profiles from a YAML profiles directory."""

    def __init__(self, profiles_dir: Optional[Path] = None) -> None:
        self._dir = profiles_dir or _DEFAULT_PROFILES_DIR
        self._faces: dict[str, Face] = {}
        self._load_all()

    # ── loading ───────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        """Parse every *.yaml / *.yml file in the profiles directory."""
        if not self._dir.exists():
            raise FileNotFoundError(
                f"Faces profiles directory not found: {self._dir}"
            )
        for path in sorted(self._dir.glob("*.yaml")) :
            self._load_file(path)
        for path in sorted(self._dir.glob("*.yml")):
            self._load_file(path)

    def _load_file(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid face profile (expected mapping): {path}")
        face = Face.model_validate(raw)
        self._faces[face.id] = face

    # ── public API ────────────────────────────────────────────────────────────

    def get_face(self, face_id: str) -> Face:
        """Return the Face for *face_id*, or raise KeyError."""
        try:
            return self._faces[face_id]
        except KeyError:
            raise KeyError(f"Unknown face id: '{face_id}'") from None

    def list_faces(self) -> list[FaceSummary]:
        """Return lightweight summaries of all loaded faces."""
        return [
            FaceSummary(
                id=f.id,
                name=f.name,
                avatar=f.avatar,
                preferred_backend=f.preferred_backend,
                ui_theme=f.ui_theme,
                display_name=f.display_name,
                blurb=f.blurb,
                simple_slot=f.simple_slot,
            )
            for f in self._faces.values()
        ]

    def get_system_prompt(self, face_id: str) -> str:
        """Return the full system prompt for *face_id*."""
        return self.get_face(face_id).system_prompt

    def get_allowed_tools(self, face_id: str) -> list[str]:
        """Return the allowed tool names for *face_id*."""
        return list(self.get_face(face_id).allowed_tools)

    def all_faces(self) -> list[Face]:
        """Return every loaded Face (full models, not summaries).

        Used by the routing-view endpoint, which needs ``role`` + ``allowed_tools``
        per face to call ``teams.resolve``. Ordered by id for stable output.
        """
        return [self._faces[fid] for fid in sorted(self._faces)]

    def __len__(self) -> int:
        return len(self._faces)

    def __contains__(self, face_id: str) -> bool:
        return face_id in self._faces


# ─── Module-level singleton accessor ──────────────────────────────────
_DEFAULT_REGISTRY: Optional["FaceRegistry"] = None


def get_default_registry() -> "FaceRegistry":
    """Return the lazily-constructed module-level FaceRegistry singleton.

    Re-reads YAML from disk only on first access; subsequent calls
    reuse the cached instance.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = FaceRegistry()
    return _DEFAULT_REGISTRY
