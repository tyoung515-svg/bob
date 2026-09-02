"""MS9-F9 — golden loader test for the FINALIZED research-forest module manifest.

The manifest ``core/modules/examples/research-forest.yaml`` is the copy-paste-ready block finalized
in MS9-F0 (F0-RECONCILIATION.md §1.3). This test proves it loads CLEAN through the *merged* MS8-M0
``load_manifest`` into a valid ``ModuleManifest`` (id=research-forest, verification_class=claim-ratchet,
pipes=[research, council]), and that mutating it to break each of the relevant M0 fail-loud rules is
REJECTED with ``ModuleManifestError``.

Mirrors the golden pattern of ``tests/modules/test_manifest.py`` (the scout golden) — F9 puts the
research-forest manifest alongside ``scout.yaml`` and validates it the same way.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from core.modules import loader as loader_mod
from core.modules.loader import ModuleManifestError, load_manifest
from core.modules.schema import ModuleManifest

# The finalized manifest lives alongside the scout golden in core/modules/examples/.
MANIFEST = Path(loader_mod.__file__).resolve().parent / "examples" / "research-forest.yaml"


# ---------------------------------------------------------------------------
# Golden: the finalized manifest loads clean into a valid ModuleManifest
# ---------------------------------------------------------------------------
def test_research_forest_manifest_loads_clean():
    m = load_manifest(MANIFEST)
    assert isinstance(m, ModuleManifest)
    # The three pins the F9 contract names explicitly.
    assert m.id == "research-forest"
    assert m.verification_class == "claim-ratchet"
    assert m.pipes == ["research", "council"]


def test_research_forest_manifest_fields():
    m = load_manifest(MANIFEST)
    # Required fields all present + F0's chosen values.
    assert m.name == "Research Forest"
    assert m.first_user == "Travis"
    assert m.lks_namespace == "research_forest"
    assert m.audience  # non-empty
    assert m.wow_flow  # non-empty
    assert m.cost_posture  # non-empty
    # faces: exactly the v1 researcher face (FaceRef with ref=, io_contract=text). NOT id=.
    assert len(m.faces) == 1
    assert m.faces[0].ref == "researcher"
    assert m.faces[0].io_contract == "text"
    # v1 surfaces no native tools (forest ops arrive later via MCP).
    assert m.tools == []
    # always_human carries the four human gates from SPEC §6.
    assert "fork approval" in m.always_human
    assert "crystallize-out merge" in m.always_human
    assert "tree archive" in m.always_human
    assert m.watch_subscriptions == []


def test_pipes_subset_of_allowed_arms():
    # SPEC §0 structural rule: pipes ⊆ {research, council} — the forest is a MODULE, not a fork.
    m = load_manifest(MANIFEST)
    assert set(m.pipes) <= {"research", "council"}


# ---------------------------------------------------------------------------
# Violation fixtures: mutate the manifest to break each relevant M0 rule → reject
# ---------------------------------------------------------------------------
@pytest.fixture
def base_manifest():
    """The finalized manifest as a plain dict (round-trips through the schema)."""
    return load_manifest(MANIFEST).model_dump()


def _dump(manifest: dict, tmp_path) -> Path:
    path = tmp_path / "m.yaml"
    with open(path, "w") as f:
        yaml.dump(manifest, f)
    return path


def test_reject_bad_io_contract(base_manifest, tmp_path):
    # R1: faces[].io_contract ∈ {text, tools_native, tools_harness}
    manifest = copy.deepcopy(base_manifest)
    manifest["faces"][0]["io_contract"] = "tools_bogus"
    with pytest.raises(ModuleManifestError, match="io_contract"):
        load_manifest(_dump(manifest, tmp_path))


def test_reject_bad_tier(base_manifest, tmp_path):
    # R2: tools[].tier ∈ {RO, Write-Local, Social, Full-Access}. v1 tools is empty, so a violation
    # fixture must ADD a tool carrying an invalid tier.
    manifest = copy.deepcopy(base_manifest)
    manifest["tools"] = [{"id": "forest-op", "tier": "Admin"}]
    with pytest.raises(ModuleManifestError, match="tier"):
        load_manifest(_dump(manifest, tmp_path))


def test_reject_bad_pipe(base_manifest, tmp_path):
    # R3: pipes ⊆ {chat, research, build, council}
    manifest = copy.deepcopy(base_manifest)
    manifest["pipes"] = ["research", "vision"]
    with pytest.raises(ModuleManifestError, match="pipes|arm"):
        load_manifest(_dump(manifest, tmp_path))


def test_reject_bad_verification_class(base_manifest, tmp_path):
    # R4: verification_class ∈ {claim-ratchet, spec-conformance, critique, human-taste}
    manifest = copy.deepcopy(base_manifest)
    manifest["verification_class"] = "vibes"
    with pytest.raises(ModuleManifestError, match="verification_class"):
        load_manifest(_dump(manifest, tmp_path))


def test_reject_missing_always_human_with_non_ro_tool(base_manifest, tmp_path):
    # R5: always_human must be non-empty when ANY tool tier ∈ {Write-Local, Social, Full-Access}.
    manifest = copy.deepcopy(base_manifest)
    manifest["tools"] = [{"id": "publish", "tier": "Full-Access"}]
    manifest["always_human"] = []
    with pytest.raises(ModuleManifestError, match="always_human"):
        load_manifest(_dump(manifest, tmp_path))


def test_reject_missing_required_field(base_manifest, tmp_path):
    # A required field dropped (min_length=1, no default) → fail-loud.
    manifest = copy.deepcopy(base_manifest)
    del manifest["name"]
    with pytest.raises(ModuleManifestError):
        load_manifest(_dump(manifest, tmp_path))
