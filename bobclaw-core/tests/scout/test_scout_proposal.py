"""MS8-SC2 — tests for the Scout absorb-proposal format (core/scout/proposal.py).

Imports core.scout.proposal DIRECTLY (the shared core/scout/__init__.py is NOT edited
by SC2 — see shared_file_requests). Fixture-driven, offline (inv. 11).

Load-bearing (inv. 13 — Scout proposes, never applies):
  * ``test_no_writes_outside_artifact_dir`` — build+write a proposal, prove the only file
    written lands inside the given artifact dir and NOTHING is written into the manifest /
    face registry source dirs.
  * ``test_nothing_registers_backend_tool_or_face`` — prove KNOWN_BACKENDS and the faces
    registry are unchanged, the absorbed tool/face are NOT present in them, the module
    exposes no register/enable surface, and the artifact's ``status`` is frozen to
    ``"proposed"`` (can never claim to be applied/registered).
"""
from pathlib import Path

import pytest
from pydantic import ValidationError

import core.config as core_config
from core.faces.registry import get_default_registry
from core.modules import loader as m0_loader
from core.modules.loader import load_manifest
from core.modules.schema import FaceRef, ModuleManifest, ToolRef
from core.scout import proposal as pm
from core.scout.proposal import (
    AbsorbCandidate,
    AbsorbProposal,
    ManifestDiff,
    build_proposal,
    load_proposal,
    write_proposal,
)

FIXTURES = Path(__file__).parent / "fixtures"
CANDIDATE = FIXTURES / "candidate_mcp_absorb.yaml"
GOLDEN = FIXTURES / "proposal_mcp_absorb.golden.yaml"
M0_GOLDEN = Path(m0_loader.__file__).resolve().parent / "examples" / "scout.yaml"

# What the candidate proposes to add (used by several assertions).
ADDED_TOOL_ID = "arxiv-mcp-fetch"
ADDED_FACE_REF = "arxiv-scout"


@pytest.fixture
def candidate() -> AbsorbCandidate:
    return AbsorbCandidate.from_yaml(CANDIDATE)


@pytest.fixture
def base() -> ModuleManifest:
    return load_manifest(M0_GOLDEN)


@pytest.fixture
def built(candidate) -> AbsorbProposal:
    # no base -> pure assembly (skips the in-memory apply validation)
    return build_proposal(candidate)


def _snapshot(d: Path) -> dict:
    """Map every non-pycache file under `d` to its size (a cheap 'nothing written' probe)."""
    return {
        p.relative_to(d).as_posix(): p.stat().st_size
        for p in d.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


# ── build + shape ────────────────────────────────────────────────────────────
def test_candidate_builds_proposal(built):
    assert isinstance(built, AbsorbProposal)
    assert built.status == "proposed"
    assert built.requires_human_merge is True
    assert built.target_module == "scout"
    assert built.manifest_diff.module_id == "scout"
    assert ADDED_TOOL_ID in {t.id for t in built.manifest_diff.added_tools}
    assert ADDED_FACE_REF in {f.ref for f in built.manifest_diff.added_faces}
    # the SC1 dossier is carried intact
    from core.scout.dossier import Dossier
    assert isinstance(built.dossier, Dossier)
    assert built.dossier.id == "arxiv-mcp-fetch-release-2026-07"


def test_build_is_deterministic_and_base_independent(candidate, base):
    # no clock / no randomness: two builds are equal; passing base (validate-only) does not
    # change the emitted artifact.
    assert build_proposal(candidate) == build_proposal(candidate)
    assert build_proposal(candidate, base=base) == build_proposal(candidate)


# ── golden ───────────────────────────────────────────────────────────────────
def test_proposal_matches_golden(built):
    golden = load_proposal(GOLDEN)
    assert built.to_dict() == golden.to_dict()
    assert built == golden


# ── round-trips ──────────────────────────────────────────────────────────────
def test_dict_round_trip(built):
    assert AbsorbProposal.from_dict(built.to_dict()) == built


def test_yaml_round_trip(built):
    assert AbsorbProposal.from_yaml_str(built.to_yaml()) == built


def test_file_round_trip(built, tmp_path):
    out = write_proposal(built, tmp_path / "artifacts")
    assert load_proposal(out) == built


def test_dossier_bundle_est_preserved_through_round_trip(built):
    reloaded = AbsorbProposal.from_dict(built.to_dict())
    for claim in reloaded.dossier.claims:
        assert claim.cost_impact.framing == "EST"
        assert claim.cost_impact.badge.endswith("(EST)")


# ── manifest-diff apply (in-memory preview) ──────────────────────────────────
def test_preview_manifest_applies_and_does_not_mutate_base(built, base):
    base_tool_ids_before = {t.id for t in base.tools}
    base_face_refs_before = {f.ref for f in base.faces}

    preview = built.preview_manifest(base)
    assert isinstance(preview, ModuleManifest)
    # the absorbed capability is present in the PREVIEW ...
    assert ADDED_TOOL_ID in {t.id for t in preview.tools}
    assert ADDED_FACE_REF in {f.ref for f in preview.faces}
    # ... 'research' pipe was already on base -> deduped (still exactly one)
    assert preview.pipes.count("research") == 1
    # ... but the BASE object is untouched (apply returns a NEW manifest)
    assert {t.id for t in base.tools} == base_tool_ids_before
    assert {f.ref for f in base.faces} == base_face_refs_before
    assert ADDED_TOOL_ID not in base_tool_ids_before


def test_apply_actuating_tool_without_always_human_fails():
    # A base with NO always_human and only RO tools is valid ...
    ro_base = ModuleManifest(
        id="m", name="m", audience="non-devs", first_user="t", wow_flow="w",
        faces=[], tools=[ToolRef(id="ro1", tier="RO")], pipes=["chat"],
        lks_namespace="m", verification_class="claim-ratchet", cost_posture="local-first",
        always_human=[], dependencies=[], watch_subscriptions=[],
    )
    # ... but absorbing a Full-Access (actuating) tool onto it must fail loud (R5 propagates
    # through apply's re-validation) — a proposal can't smuggle in an actuating capability
    # without the human-gate field.
    diff = ManifestDiff(
        module_id="m", base_ref="x",
        added_tools=[ToolRef(id="publish", tier="Full-Access")],
    )
    with pytest.raises(ValueError, match="always_human"):
        diff.apply(ro_base)


def test_apply_rejects_tool_id_colliding_with_base(base):
    existing = next(iter(base.tools)).id
    diff = ManifestDiff(
        module_id="scout", base_ref="x",
        added_tools=[ToolRef(id=existing, tier="RO")],
    )
    with pytest.raises(ValueError, match="already exists in base manifest"):
        diff.apply(base)


# ── ManifestDiff validation (reuses M0 rules) ────────────────────────────────
def test_diff_rejects_bad_pipe():
    with pytest.raises(ValueError, match="added_pipes"):
        ManifestDiff(module_id="m", base_ref="x", added_pipes=["vision"])


def test_diff_rejects_bad_tool_tier():
    with pytest.raises(ValueError, match="tier"):
        ManifestDiff(module_id="m", base_ref="x", added_tools=[{"id": "t", "tier": "Admin"}])


def test_diff_rejects_bad_io_contract():
    with pytest.raises(ValueError, match="io_contract"):
        ManifestDiff(module_id="m", base_ref="x",
                     added_faces=[{"ref": "f", "io_contract": "tools_bogus"}])


def test_empty_diff_rejected():
    with pytest.raises(ValueError, match="at least one capability"):
        ManifestDiff(module_id="m", base_ref="x")


def test_diff_rejects_internal_duplicate_tool():
    with pytest.raises(ValueError, match="duplicate tool id"):
        ManifestDiff(module_id="m", base_ref="x",
                     added_tools=[{"id": "t", "tier": "RO"}, {"id": "t", "tier": "RO"}])


def test_diff_rejects_empty_module_id():
    with pytest.raises(ValueError, match="non-empty"):
        ManifestDiff(module_id="  ", base_ref="x", added_pipes=["chat"])


def test_diff_rejects_empty_string_dependency():
    # a `[""]` entry must not masquerade as a real addition (audit r1, focus 1)
    with pytest.raises(ValueError, match="non-empty strings"):
        ManifestDiff(module_id="m", base_ref="x", added_dependencies=[""])


def test_diff_rejects_empty_string_watch_subscription():
    with pytest.raises(ValueError, match="non-empty strings"):
        ManifestDiff(module_id="m", base_ref="x", added_watch_subscriptions=["   "])


# ── AbsorbProposal invariants ────────────────────────────────────────────────
def test_status_literal_rejects_non_proposed(built):
    bad = {**built.to_dict(), "status": "registered"}
    with pytest.raises(ValidationError):
        AbsorbProposal.from_dict(bad)


def test_requires_human_merge_rejects_false(built):
    bad = {**built.to_dict(), "requires_human_merge": False}
    with pytest.raises(ValidationError):
        AbsorbProposal.from_dict(bad)


def test_target_module_must_match_diff(built):
    bad = {**built.to_dict(), "target_module": "not-scout"}
    with pytest.raises(ValueError, match="must match manifest_diff.module_id"):
        AbsorbProposal.from_dict(bad)


def test_bad_schema_version(built):
    bad = {**built.to_dict(), "schema_version": 1}
    with pytest.raises(ValueError, match="schema_version must be 0"):
        AbsorbProposal.from_dict(bad)


def test_extra_field_forbidden(built):
    bad = {**built.to_dict(), "surprise": "x"}
    with pytest.raises(ValueError, match="[Ee]xtra"):
        AbsorbProposal.from_dict(bad)


@pytest.mark.parametrize("evil_id", ["../escape", "foo/bar", "a\\b", "..", "."])
def test_id_rejects_path_traversal(built, evil_id):
    # inv. 13: a proposal id is used as a filename; path separators / traversal are rejected
    # at author time so write_proposal can never escape the artifact dir.
    bad = {**built.to_dict(), "id": evil_id}
    with pytest.raises(ValidationError):
        AbsorbProposal.from_dict(bad)


# ── inv. 13 (load-bearing): proposal-only, nothing registers, no stray writes ─
def test_no_writes_outside_artifact_dir(candidate, base, tmp_path, monkeypatch):
    core_dir = Path(pm.__file__).resolve().parent.parent          # .../bobclaw-core/core
    examples_dir = core_dir / "modules" / "examples"              # where manifests live
    faces_dir = core_dir / "faces" / "profiles"                   # where face YAML lives
    examples_before = _snapshot(examples_dir)
    faces_before = _snapshot(faces_dir)

    artifact_dir = tmp_path / "artifacts"

    # Instrument EVERY filesystem mutation (Path.write_text + Path.mkdir) so the proof is
    # airtight AND immune to concurrent sprints writing elsewhere in the tree: we observe only
    # the paths THIS code touches, and assert every one is confined to artifact_dir.
    touched: list[Path] = []
    real_write_text = Path.write_text
    real_mkdir = Path.mkdir

    def spy_write_text(self, *a, **k):
        touched.append(Path(self))
        return real_write_text(self, *a, **k)

    def spy_mkdir(self, *a, **k):
        touched.append(Path(self))
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    monkeypatch.setattr(Path, "mkdir", spy_mkdir)

    prop = build_proposal(candidate, base=base)                   # exercises apply() too
    out = write_proposal(prop, artifact_dir)

    monkeypatch.undo()

    # every path the code created/wrote is artifact_dir itself or a file directly inside it
    ad_resolved = artifact_dir.resolve()
    assert touched, "expected at least the mkdir + write_text to be observed"
    for p in touched:
        rp = p.resolve()
        assert rp == ad_resolved or rp.parent == ad_resolved, f"stray filesystem write: {rp}"
    # build_proposal itself wrote NOTHING; only write_proposal did (mkdir + the one file)
    assert out in touched and out.resolve().parent == ad_resolved

    # the artifact landed ONLY inside the given artifact dir
    assert out.parent == artifact_dir
    assert sorted(p.name for p in artifact_dir.iterdir()) == [out.name]
    assert out.read_text(encoding="utf-8").strip()               # non-empty artifact

    # and the manifest / face source dirs were not written into (registration surfaces)
    assert _snapshot(examples_dir) == examples_before
    assert _snapshot(faces_dir) == faces_before


def test_nothing_registers_backend_tool_or_face(candidate, base, tmp_path):
    backends_before = set(core_config.KNOWN_BACKENDS)
    faces_before = {f.id for f in get_default_registry().all_faces()}

    prop = build_proposal(candidate, base=base)
    write_proposal(prop, tmp_path / "artifacts")
    prop.preview_manifest(base)   # in-memory apply — still must register nothing

    # the absorbed tool did NOT become a backend; the registry is byte-for-byte the same set
    assert set(core_config.KNOWN_BACKENDS) == backends_before
    assert ADDED_TOOL_ID not in core_config.KNOWN_BACKENDS

    # the absorbed face did NOT enter the faces registry
    faces_after = {f.id for f in get_default_registry().all_faces()}
    assert faces_after == faces_before
    assert ADDED_FACE_REF not in faces_after

    # the module exposes NO registration / enablement surface — it is proposal-only
    for banned in (
        "register", "enable", "activate", "install",
        "register_backend", "register_tool", "register_face", "apply_to_registry",
    ):
        assert not hasattr(pm, banned), f"proposal module exposes a registration surface: {banned}"

    # and the artifact can NEVER claim to be applied/registered (status frozen to 'proposed')
    with pytest.raises(ValidationError):
        AbsorbProposal.from_dict({**prop.to_dict(), "status": "applied"})
