"""test_publish_gists.py — the core suite pins the release-cut publish mirror.

Loads scripts/publish_gists.py (it lives at the repo root, off the core import path)
and exercises the GIST-SHAPES.md §9/D19 distribution filter against fixture gist trees
built in tmp_path (nothing here touches the real repo or the real bob tree):

  (1) the eligible set is EXACTLY status:published AND distribution:public — shaped /
      internal / proposed gists are excluded;
  (2) landings ride along only for a public receiver, and only when they are evidence
      for an eligible gist;
  (3) the mirror is idempotent — a second run over the same target writes nothing;
  (4) the bob-guard refuses a target inside C:\\dev\\projects\\bob unless --i-am-travis,
      tested with an injected sandbox bob-tree (full refuse/allow cycle in tmp_path) AND
      a pure path-containment assertion against the real constant (no disk I/O).

The standard being mirrored is tasks/2026-07-07-gist-shapes/GIST-SHAPES.md §9.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_PUBLISH_PATH = REPO_ROOT / "scripts" / "publish_gists.py"


def _load_publisher():
    spec = importlib.util.spec_from_file_location("publish_gists", _PUBLISH_PATH)
    assert spec and spec.loader, f"cannot load publisher at {_PUBLISH_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pg = _load_publisher()


# ── fixture builders ─────────────────────────────────────────────────────────
def _gist(num: str, slug: str, *, status: str, distribution: str) -> str:
    """A minimal but frontmatter-parseable gist. publish_gists filters on status +
    distribution only, so the body is a placeholder — this is a mirror, not a validator."""
    return (
        "---\n"
        f"id: gist-{num}-{slug}\n"
        f"title: {slug} sample\n"
        "criticality: capability\n"
        "verification_class: spec-conformance\n"
        "transfer: shaped\n"
        f"distribution: {distribution}\n"
        "intended: [bobclaw, users]\n"
        "provenance:\n"
        "  source_tree: bobclaw\n"
        "  commit: deadbee\n"
        "requires:\n"
        "  capabilities: []\n"
        f"status: {status}\n"
        "---\n"
        "## Intent\nplaceholder\n"
    )


def _landing(gist_id: str) -> str:
    return (
        "---\n"
        f"gist: {gist_id}\n"
        "status: landed\n"
        "---\n"
        "| criterion | check | result | tag |\n"
        "|---|---|---|---|\n"
        "| A1: does the thing | test_x | pass | PV |\n"
    )


def _build_source(root: Path) -> Path:
    """A source gists/ tree with a deliberate mix of status/distribution + landings."""
    src = root / "gists"
    (src / "landings").mkdir(parents=True)
    # eligible: published + public
    (src / "0001-alpha.md").write_text(
        _gist("0001", "alpha", status="published", distribution="public"), encoding="utf-8"
    )
    (src / "0005-echo.md").write_text(
        _gist("0005", "echo", status="published", distribution="public"), encoding="utf-8"
    )
    # NOT eligible — shaped (not yet published)
    (src / "0002-bravo.md").write_text(
        _gist("0002", "bravo", status="shaped", distribution="public"), encoding="utf-8"
    )
    # NOT eligible — published but internal distribution
    (src / "0003-charlie.md").write_text(
        _gist("0003", "charlie", status="published", distribution="internal"),
        encoding="utf-8",
    )
    # NOT eligible — proposed
    (src / "0004-delta.md").write_text(
        _gist("0004", "delta", status="proposed", distribution="public"), encoding="utf-8"
    )
    # a README (no frontmatter) must never be treated as a gist
    (src / "README.md").write_text("what this dir is\n", encoding="utf-8")
    # landings: one for an eligible gist, one for an excluded gist
    (src / "landings" / "0001-bobclaw.md").write_text(
        _landing("gist-0001-alpha"), encoding="utf-8"
    )
    (src / "landings" / "0002-bobclaw.md").write_text(
        _landing("gist-0002-bravo"), encoding="utf-8"
    )
    return src


ELIGIBLE = {"0001-alpha.md", "0005-echo.md"}
EXCLUDED_GISTS = {"0002-bravo.md", "0003-charlie.md", "0004-delta.md", "README.md"}


def _target_tree(target: Path) -> dict[str, bytes]:
    """Snapshot every file under target as {relative_posix_path: bytes}."""
    return {
        p.relative_to(target).as_posix(): p.read_bytes()
        for p in sorted(target.rglob("*"))
        if p.is_file()
    }


# ── (1) exactly the eligible set is copied ───────────────────────────────────
def test_copies_exactly_the_eligible_set(tmp_path):
    src = _build_source(tmp_path / "truth")
    target = tmp_path / "out"
    result = pg.publish_gists(src, target)

    copied = {p.name for p in target.glob("*.md")}
    assert copied == ELIGIBLE, f"copied set wrong: {copied}"
    assert result.eligible_gists == sorted(ELIGIBLE)
    # every excluded gist is named in the report and absent from the target
    for name in EXCLUDED_GISTS:
        assert not (target / name).exists(), f"{name} must NOT be published"
    assert any("0002-bravo.md" in e for e in result.excluded)      # shaped
    assert any("0003-charlie.md" in e for e in result.excluded)    # internal
    assert any("0004-delta.md" in e for e in result.excluded)      # proposed


# ── (2) landings: public receiver only, matching gists only ──────────────────
def test_landings_excluded_for_non_public_receiver(tmp_path):
    src = _build_source(tmp_path / "truth")
    target = tmp_path / "out"
    result = pg.publish_gists(src, target, public_tree=False)
    assert not (target / "landings").exists(), "no landings for a non-public receiver"
    assert result.published_landings == []


def test_landings_included_for_public_tree_only_when_matching(tmp_path):
    src = _build_source(tmp_path / "truth")
    target = tmp_path / "out"
    result = pg.publish_gists(src, target, public_tree=True)
    landed = {p.name for p in (target / "landings").glob("*.md")}
    # only the landing whose gist (gist-0001-alpha) is in the eligible set travels;
    # the landing for the shaped gist-0002-bravo does not.
    assert landed == {"0001-bobclaw.md"}, f"landing set wrong: {landed}"
    assert result.published_landings == ["0001-bobclaw.md"]
    assert not (target / "landings" / "0002-bobclaw.md").exists()


# ── (3) idempotency: a second run changes nothing ────────────────────────────
def test_idempotent_second_run_is_a_noop(tmp_path):
    src = _build_source(tmp_path / "truth")
    target = tmp_path / "out"

    first = pg.publish_gists(src, target, public_tree=True)
    assert first.changed is True
    snapshot = _target_tree(target)
    mtimes = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}

    second = pg.publish_gists(src, target, public_tree=True)
    assert second.changed is False, "re-run must write nothing"
    assert second.gists_written == [] and second.landings_written == []
    # byte-identical tree, and no file was rewritten (mtimes preserved)
    assert _target_tree(target) == snapshot
    assert {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()} == mtimes


# ── (4) the bob-guard ────────────────────────────────────────────────────────
def test_bob_guard_refuses_without_travis_flag(tmp_path):
    """Injected sandbox bob-tree: a target inside it is refused, and NOTHING is written."""
    src = _build_source(tmp_path / "truth")
    fake_bob = tmp_path / "bob"
    target = fake_bob / "gists"  # resolves inside the (sandbox) bob tree
    with pytest.raises(pg.BobGuardRefusal):
        pg.publish_gists(src, target, i_am_travis=False, bob_tree=fake_bob)
    assert not fake_bob.exists(), "a refused publish must not create anything under bob"


def test_bob_guard_allows_with_travis_flag(tmp_path):
    """Same injected sandbox tree, but --i-am-travis lets the attended cut through."""
    src = _build_source(tmp_path / "truth")
    fake_bob = tmp_path / "bob"
    target = fake_bob / "gists"
    result = pg.publish_gists(src, target, i_am_travis=True, bob_tree=fake_bob)
    assert {p.name for p in target.glob("*.md")} == ELIGIBLE


def test_bob_guard_path_containment_against_real_constant():
    """Pure path assertion against the REAL C:\\dev\\projects\\bob constant — no disk I/O,
    so the invariant 'never write into bob in-sprint' is honored while still pinning that
    a genuine bob path would be caught."""
    assert pg.resolves_inside_bob(pg.BOB_TREE / "gists")
    assert pg.resolves_inside_bob(pg.BOB_TREE)  # the tree root itself
    assert pg.resolves_inside_bob(pg.BOB_TREE / "gists" / "landings" / "x.md")
    assert not pg.resolves_inside_bob(Path(r"C:\dev\projects\bobclaw\gists"))
    assert not pg.resolves_inside_bob(Path(r"C:\dev\projects\bob-other\gists"))


def test_cli_refusal_exit_code(tmp_path, capsys):
    """The CLI returns non-zero and prints a REFUSED line for a real bob-tree target,
    using a synthetic subpath under the real constant (no write is attempted)."""
    src = _build_source(tmp_path / "truth")
    # a path under the real bob tree; the guard trips before any write is attempted.
    target = pg.BOB_TREE / "gists"
    rc = pg.main([str(src), str(target)])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().out


def test_cli_happy_path_exit_zero(tmp_path, capsys):
    src = _build_source(tmp_path / "truth")
    target = tmp_path / "out"
    rc = pg.main([str(src), str(target), "--public-tree"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "publish:" in out
    assert {p.name for p in target.glob("*.md")} == ELIGIBLE
