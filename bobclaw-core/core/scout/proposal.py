"""BoBClaw Scout absorb-proposal format (module M1 / MS8-SC2).

The *absorb artifact*: a **manifest diff** (M0 schema, ``core.modules.schema``)
bundled with a Scout **evidence dossier** (SC1, ``core.scout.dossier``). This is
what Scout emits when it "absorbs" a newly-found tool/face/backend.

**Proposal-ONLY (inv. 13 / MODULES.md D1).** Scout PROPOSES, never applies. Nothing
here registers a backend, tool, or face; nothing writes into the source tree or any
live registry. An ``AbsorbProposal`` is a pure data artifact whose ``status`` is
frozen to ``"proposed"`` and whose ``requires_human_merge`` is frozen to ``True`` —
a human merge (enabling the manifest diff) is the ONLY thing that activates anything.

Pure by construction: no network (inv. 11 — ``base_ref`` is a provenance path, never
fetched), no clock (``created_at`` is carried from the candidate, never sampled), no
randomness, no global mutation. ``ManifestDiff.apply`` is an IN-MEMORY preview that
re-validates through the M0 ``ModuleManifest`` schema (so a diff that WOULD produce an
invalid manifest — e.g. an actuating tool with no ``always_human`` — fails loud at
proposal time) and returns a NEW manifest without mutating its input.

Round-trip contract (mirrors SC1's Dossier)::

    AbsorbProposal.from_dict(p.to_dict()) == p
    AbsorbProposal.from_yaml_str(p.to_yaml()) == p
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.modules.schema import FaceRef, ModuleManifest, ToolRef
from core.scout.dossier import Dossier


VALID_PIPES = {"chat", "research", "build", "council"}
SCHEMA_VERSION = 0

# A proposal ``id`` also NAMES the artifact file (``{id}.proposal.yaml``). It must be a
# filesystem-safe slug with NO path separators / traversal so ``write_proposal`` can never
# escape the caller-supplied artifact dir (inv. 13).
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


class ManifestDiff(BaseModel):
    """An ADDITIVE proposed change to a module manifest (M0 schema). Absorb only ever
    ADDS capabilities to an existing module; it never rewrites scalar identity fields."""
    model_config = ConfigDict(extra="forbid")

    module_id: str
    base_ref: str                                  # provenance path to the base manifest; NEVER fetched (inv. 11)
    added_faces: list[FaceRef] = Field(default_factory=list)
    added_tools: list[ToolRef] = Field(default_factory=list)
    added_pipes: list[str] = Field(default_factory=list)
    added_dependencies: list[str] = Field(default_factory=list)
    added_watch_subscriptions: list[str] = Field(default_factory=list)

    # ---- field validators ----
    @field_validator("module_id", "base_ref")
    @classmethod
    def non_empty_string(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError(f"field must be a non-empty string (got '{v}')")
        return stripped

    @field_validator("added_pipes")
    @classmethod
    def pipes_in_valid_set(cls, v: list[str]) -> list[str]:
        # the no-new-arms rule (MODULES.md §2) as a validator, mirroring M0's pipes check
        for pipe in v:
            if pipe not in VALID_PIPES:
                raise ValueError(f"added_pipes must contain only pipes in {VALID_PIPES}; got '{pipe}'")
        return v

    @field_validator("added_dependencies", "added_watch_subscriptions")
    @classmethod
    def non_empty_str_entries(cls, v: list[str]) -> list[str]:
        # reject empty / whitespace-only entries so a `[""]` cannot masquerade as a real
        # addition and slip past `require_some_addition`.
        cleaned: list[str] = []
        for item in v:
            s = item.strip()
            if not s:
                raise ValueError(
                    "added_dependencies / added_watch_subscriptions entries must be "
                    "non-empty strings"
                )
            cleaned.append(s)
        return cleaned

    # ---- model validators ----
    @model_validator(mode="after")
    def require_some_addition(self) -> "ManifestDiff":
        if not any([
            self.added_faces,
            self.added_tools,
            self.added_pipes,
            self.added_dependencies,
            self.added_watch_subscriptions,
        ]):
            raise ValueError("a manifest diff must add at least one capability (empty diff)")
        return self

    @model_validator(mode="after")
    def no_internal_dupes(self) -> "ManifestDiff":
        seen_tool_ids: set[str] = set()
        for tool in self.added_tools:
            if tool.id in seen_tool_ids:
                raise ValueError(f"duplicate tool id in added_tools: '{tool.id}'")
            seen_tool_ids.add(tool.id)

        seen_face_refs: set[str] = set()
        for face in self.added_faces:
            if face.ref in seen_face_refs:
                raise ValueError(f"duplicate face ref in added_faces: '{face.ref}'")
            seen_face_refs.add(face.ref)

        return self

    # ---- apply (in-memory preview; never writes, never registers, never mutates base) ----
    def apply(self, base: ModuleManifest) -> ModuleManifest:
        """Return a NEW ``ModuleManifest`` = ``base`` + these additions, RE-VALIDATED
        through the M0 ``ModuleManifest`` schema. IN-MEMORY ONLY: never writes, never
        registers, never mutates ``base``. Fail loud on a collision (an added tool id or
        face ref already present in ``base``). Pipes / dependencies / watch_subscriptions
        are appended order-stable with dedup. Because the result is re-validated by
        ``ModuleManifest``, absorbing an actuating tool (tier != RO) onto a base with
        empty ``always_human`` fails loud here (R5)."""
        merged = base.model_dump()  # base.faces/tools become lists of plain dicts

        # collision checks BEFORE merging
        existing_face_refs = {f.ref for f in base.faces}
        for f in self.added_faces:
            if f.ref in existing_face_refs:
                raise ValueError(f"face ref '{f.ref}' already exists in base manifest")
        existing_tool_ids = {t.id for t in base.tools}
        for t in self.added_tools:
            if t.id in existing_tool_ids:
                raise ValueError(f"tool id '{t.id}' already exists in base manifest")

        # merge faces + tools as plain dicts (keeps `merged` uniformly dict-shaped)
        merged["faces"] = merged["faces"] + [f.model_dump() for f in self.added_faces]
        merged["tools"] = merged["tools"] + [t.model_dump() for t in self.added_tools]

        def dedup_extend(base_list: list[str], added_list: list[str]) -> list[str]:
            seen = set(base_list)
            result = list(base_list)
            for item in added_list:
                if item not in seen:
                    result.append(item)
                    seen.add(item)
            return result

        merged["pipes"] = dedup_extend(base.pipes, self.added_pipes)
        merged["dependencies"] = dedup_extend(base.dependencies, self.added_dependencies)
        merged["watch_subscriptions"] = dedup_extend(
            base.watch_subscriptions, self.added_watch_subscriptions
        )

        # re-validate through M0 ModuleManifest (catches R5 and every author-time rule)
        return ModuleManifest.model_validate(merged)


class AbsorbProposal(BaseModel):
    """The absorb artifact: a manifest diff (M0 schema) bundled with a Scout evidence
    Dossier (SC1). Proposal-ONLY (inv. 13): ``status`` is frozen to ``"proposed"`` and
    ``requires_human_merge`` to ``True`` — the artifact can NEVER claim to be
    applied/registered."""
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 0
    id: str
    title: str
    summary: str = ""
    target_module: str
    status: Literal["proposed"] = "proposed"          # inv.13 marker: proposal-only, immutable
    requires_human_merge: Literal[True] = True         # MODULES.md D1 / M1 ALWAYS-HUMAN: a human merge enables
    manifest_diff: ManifestDiff
    dossier: Dossier                                   # SC1 evidence bundle, carried intact
    created_at: Optional[str] = None

    # ---- field validators ----
    @field_validator("schema_version")
    @classmethod
    def version_must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError(f"schema_version must be 0, got {v} (unsupported)")
        return v

    @field_validator("title")
    @classmethod
    def title_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError(f"title must be a non-empty string (got '{v}')")
        return stripped

    @field_validator("id")
    @classmethod
    def id_is_safe_slug(cls, v: str) -> str:
        # inv. 13: `id` names the artifact file (`{id}.proposal.yaml`); a caller/YAML-supplied
        # id must never carry a path separator or `..`, or write_proposal could escape the
        # artifact dir. Enforce a filesystem-safe slug here (fail loud at author time).
        s = v.strip()
        if not s:
            raise ValueError("id must be a non-empty string")
        if s in {".", ".."} or ".." in s or not _SAFE_ID_RE.fullmatch(s):
            raise ValueError(
                f"id must be a filesystem-safe slug matching [A-Za-z0-9._-]+ (not '.'/'..', "
                f"no '..', no path separators — inv. 13), got {v!r}"
            )
        return s

    # ---- model validators ----
    @model_validator(mode="after")
    def target_matches_diff(self) -> "AbsorbProposal":
        if self.target_module != self.manifest_diff.module_id:
            raise ValueError(
                f"target_module '{self.target_module}' must match manifest_diff.module_id "
                f"'{self.manifest_diff.module_id}'"
            )
        return self

    # ---- serialisation helpers (mirror SC1 Dossier) ----
    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict) -> "AbsorbProposal":
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml_str(cls, text: str) -> "AbsorbProposal":
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("proposal YAML root must be a mapping")
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: Path) -> "AbsorbProposal":
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_yaml_str(text)

    # ---- convenience preview (in-memory only) ----
    def preview_manifest(self, base: ModuleManifest) -> ModuleManifest:
        """IN-MEMORY preview of what a human merge WOULD produce =
        ``self.manifest_diff.apply(base)``. Convenience only; writes/registers nothing."""
        return self.manifest_diff.apply(base)


class AbsorbCandidate(BaseModel):
    """The raw Scout finding that :func:`build_proposal` turns into an
    :class:`AbsorbProposal`: what was found + what it would add to which module + the
    evidence dossier."""
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    summary: str = ""
    target_module: str
    base_ref: str
    added_faces: list[FaceRef] = Field(default_factory=list)
    added_tools: list[ToolRef] = Field(default_factory=list)
    added_pipes: list[str] = Field(default_factory=list)
    added_dependencies: list[str] = Field(default_factory=list)
    added_watch_subscriptions: list[str] = Field(default_factory=list)
    dossier: Dossier
    created_at: Optional[str] = None

    @classmethod
    def from_yaml(cls, path: Path) -> "AbsorbCandidate":
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("candidate YAML root must be a mapping")
        return cls.model_validate(data)


# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------

def build_proposal(
    candidate: AbsorbCandidate, base: Optional[ModuleManifest] = None
) -> AbsorbProposal:
    """Deterministically assemble the proposal-only absorb artifact.

    NO clock, NO network, NO registration. Organises the candidate's flat ``added_*``
    into a validated :class:`ManifestDiff`, stamps the proposal-only invariants
    (``status="proposed"``, ``requires_human_merge=True``), and bundles the dossier.
    If ``base`` is supplied, the diff is validated by applying it in-memory (raises if
    the merge would be invalid) and the applied manifest is DISCARDED — nothing is
    written or registered."""
    diff = ManifestDiff(
        module_id=candidate.target_module,
        base_ref=candidate.base_ref,
        added_faces=candidate.added_faces,
        added_tools=candidate.added_tools,
        added_pipes=candidate.added_pipes,
        added_dependencies=candidate.added_dependencies,
        added_watch_subscriptions=candidate.added_watch_subscriptions,
    )

    if base is not None:
        diff.apply(base)  # validate-only; result discarded (side-effect free)

    return AbsorbProposal(
        id=candidate.id,
        title=candidate.title,
        summary=candidate.summary,
        target_module=candidate.target_module,
        manifest_diff=diff,
        dossier=candidate.dossier,
        created_at=candidate.created_at,
    )


def write_proposal(proposal: AbsorbProposal, artifact_dir) -> Path:
    """Serialise ``proposal`` to ``Path(artifact_dir)/f"{proposal.id}.proposal.yaml"``.

    Creates ``artifact_dir`` (parents ok) if missing and writes ONLY inside it. Returns
    the written Path. Writes nothing into the source tree; registers nothing (inv. 13)."""
    dir_path = Path(artifact_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{proposal.id}.proposal.yaml"
    # inv. 13 defence-in-depth: even if id validation were bypassed, refuse to write anywhere
    # but DIRECTLY inside artifact_dir (the artifact is a single flat file).
    if file_path.resolve().parent != dir_path.resolve():
        raise ValueError(
            f"refusing to write outside artifact_dir (inv. 13): resolved {file_path.resolve()} "
            f"is not directly in {dir_path.resolve()}"
        )
    file_path.write_text(proposal.to_yaml(), encoding="utf-8")
    return file_path


def load_proposal(path) -> AbsorbProposal:
    """Load and validate an :class:`AbsorbProposal` from a YAML file path."""
    return AbsorbProposal.from_yaml(Path(path))
