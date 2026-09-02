#!/usr/bin/env python3
"""publish_gists.py — the release-cut distribution mirror for gist shapes.

Implements GIST-SHAPES.md (`tasks/2026-07-07-gist-shapes/GIST-SHAPES.md`) §9/D19:
`bobclaw/gists/` is TRUTH; distribution is a *filter* on two frontmatter fields —
`status: published` AND `distribution: public` — mirrored into a receiver tree's
`gists/` at release cut. Landings ride along ONLY when the receiver is a public tree
(bob's own landings are public; an internal/enterprise receiver keeps its landings
internal). This script is that filter-and-copy motion, made mechanical.

Design invariants (mirror scripts/check_hygiene.py + scripts/check_gists.py house style):
  * Frontmatter parsing is REUSED from scripts/check_gists.py (imported read-only) so
    the publish filter reads the exact same `status` / `distribution` fields the
    validator does — no second, drifting parser.
  * NO CWD / GLOBAL STATE — every motion keys off explicit source/target Paths.
  * IDEMPOTENT — a file is written only when its bytes differ from what is already at
    the target, so a re-run over the same target produces no filesystem change (no diff,
    unchanged mtimes). The mirror is copy-only; it NEVER deletes at the target.
  * bob-GUARD (attended-release-cut safety): the script REFUSES to write if the target
    resolves inside `C:\\dev\\projects\\bob` unless `--i-am-travis` is passed. The guard
    is a pure path-containment test (no disk write, no stat of the bob tree), so it can
    be exercised against a synthetic path in-suite without ever touching the real tree.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── reuse the validator's frontmatter parser (read-only import) ───────────────
# publish_gists.py lives next to check_gists.py in scripts/; put that dir on the
# path so `import check_gists` resolves whether we run as a script (dir already on
# sys.path[0]) or get imported by the core test via importlib.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import check_gists  # noqa: E402  (sibling script; imported read-only for its parser)

# ── the bob-guard tree (§1 addendum invariant: never write here in-sprint) ────
# The public OSS clean-room. The mirror step Travis runs attended at a release cut is
# the ONE sanctioned write into this tree, gated behind --i-am-travis.
BOB_TREE = Path(r"C:\dev\projects\bob")

# The publishable set (GIST-SHAPES.md §9/D19): status published AND distribution public.
PUBLISH_STATUS = "published"
PUBLISH_DISTRIBUTION = "public"


class BobGuardRefusal(RuntimeError):
    """Raised when a publish target resolves inside the bob tree without --i-am-travis."""


# ── path containment (pure, hermetic — no disk I/O, no writes) ────────────────
def _canon(p: Path | str) -> str:
    """Case-insensitive, separator-normalized absolute form for containment tests.
    Uses os.path.abspath (pure string normalization vs CWD), NEVER resolve(), so it
    touches no filesystem — the bob-guard must be assertable without stat-ing bob."""
    return os.path.normcase(os.path.abspath(str(p)))


def resolves_inside_bob(target: Path | str, bob_tree: Path | str = BOB_TREE) -> bool:
    """True when `target` is the bob tree root or any path beneath it. `bob_tree` is
    injectable so tests exercise the guard against a synthetic sandbox root, never the
    real C:\\dev\\projects\\bob."""
    tgt = _canon(target)
    bob = _canon(bob_tree)
    if tgt == bob:
        return True
    return tgt.startswith(bob + os.sep)


# ── result model ──────────────────────────────────────────────────────────────
@dataclass
class PublishResult:
    """What one publish motion did. Idempotency shows up as everything landing in the
    *_unchanged lists on a re-run (changed == False)."""

    source: Path
    target: Path
    public_tree: bool
    gists_written: list[str] = field(default_factory=list)     # created or content-updated
    gists_unchanged: list[str] = field(default_factory=list)   # already byte-identical
    landings_written: list[str] = field(default_factory=list)
    landings_unchanged: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)          # "name (reason)"

    @property
    def eligible_gists(self) -> list[str]:
        """The exact set of gist files this run considers publishable (copied set)."""
        return sorted(self.gists_written + self.gists_unchanged)

    @property
    def published_landings(self) -> list[str]:
        return sorted(self.landings_written + self.landings_unchanged)

    @property
    def changed(self) -> bool:
        """False ⇒ the target already matched (idempotent no-op re-run)."""
        return bool(self.gists_written or self.landings_written)

    def summary(self) -> str:
        return (
            f"publish: {len(self.eligible_gists)} eligible gist(s) "
            f"({len(self.gists_written)} written, {len(self.gists_unchanged)} unchanged), "
            f"{len(self.published_landings)} landing(s) "
            f"({len(self.landings_written)} written, {len(self.landings_unchanged)} unchanged), "
            f"{len(self.excluded)} excluded  [public_tree={self.public_tree}]"
        )


# ── helpers ───────────────────────────────────────────────────────────────────
def _load_frontmatter(path: Path) -> Optional[dict]:
    """Parse a gist/landing's YAML frontmatter with check_gists' parser (same fields
    the validator reads). Returns None if the file has no parseable frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, _body, err = check_gists.split_frontmatter(text)
    if err is not None or fm is None:
        return None
    return fm


def _is_publishable(fm: dict) -> bool:
    """GIST-SHAPES §9/D19: status published AND distribution public — both required."""
    return (
        fm.get("status") == PUBLISH_STATUS
        and fm.get("distribution") == PUBLISH_DISTRIBUTION
    )


def _referenced_gist_id(path: Path, fm: Optional[dict]) -> Optional[str]:
    """The gist id a landing is evidence for: a frontmatter `gist:` field, else the
    first `gist-NNNN-slug` reference in the body (mirrors check_gists' cross-check)."""
    if fm and fm.get("gist"):
        return str(fm["gist"])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = check_gists.GIST_REF_RE.search(text)
    return m.group(0) if m else None


def _mirror_file(src: Path, dst: Path) -> bool:
    """Copy src→dst, but only when bytes differ. Returns True if it wrote (created or
    updated), False if the target was already identical — this is what makes a re-run a
    no-op (idempotency)."""
    payload = src.read_bytes()
    if dst.exists() and dst.read_bytes() == payload:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(payload)
    return True


# ── the publish motion ────────────────────────────────────────────────────────
def publish_gists(
    source: Path | str,
    target: Path | str,
    *,
    public_tree: bool = False,
    i_am_travis: bool = False,
    bob_tree: Path | str = BOB_TREE,
) -> PublishResult:
    """Mirror the published+public gists from `source` into `target`.

    - `public_tree`: the receiver is a public distribution tree (e.g. bob) ⇒ matching
      landings (evidence for an eligible gist) are mirrored too; otherwise landings stay
      internal and are not copied (GIST-SHAPES §9).
    - bob-guard: refuses (BobGuardRefusal) if `target` is inside `bob_tree` unless
      `i_am_travis`. Checked FIRST, before any directory is created — a refused run
      writes nothing.
    """
    source = Path(source)
    target = Path(target)

    # bob-guard FIRST — a refusal must create nothing at or near the target.
    if resolves_inside_bob(target, bob_tree) and not i_am_travis:
        raise BobGuardRefusal(
            f"refusing to publish into the bob tree: target {target} resolves inside "
            f"{Path(bob_tree)} (pass --i-am-travis for the attended release cut only)"
        )

    if not source.is_dir():
        raise FileNotFoundError(f"source gists dir does not exist: {source}")

    result = PublishResult(source=source, target=target, public_tree=public_tree)

    # (1) select + mirror the eligible gists (top-level *.md, README excluded — same
    #     convention as check_gists.validate_dir).
    eligible_ids: set[str] = set()
    for src in sorted(source.glob("*.md")):
        if src.name.lower() == "readme.md":
            continue
        fm = _load_frontmatter(src)
        if fm is None:
            result.excluded.append(f"{src.name} (no parseable frontmatter)")
            continue
        if not _is_publishable(fm):
            reason = (
                f"status={fm.get('status')!r} distribution={fm.get('distribution')!r}"
            )
            result.excluded.append(f"{src.name} (not published+public: {reason})")
            continue
        gid = fm.get("id")
        if gid:
            eligible_ids.add(str(gid))
        wrote = _mirror_file(src, target / src.name)
        (result.gists_written if wrote else result.gists_unchanged).append(src.name)

    # (2) landings ride along ONLY for a public receiver, and ONLY when the landing is
    #     evidence for one of the eligible gists (GIST-SHAPES §9: matching landings).
    src_landings = source / "landings"
    if src_landings.is_dir():
        for src in sorted(src_landings.glob("*.md")):
            if src.name.lower() == "readme.md":
                continue
            if not public_tree:
                result.excluded.append(
                    f"landings/{src.name} (receiver is not a public tree)"
                )
                continue
            fm = _load_frontmatter(src)
            gid = _referenced_gist_id(src, fm)
            if gid not in eligible_ids:
                result.excluded.append(
                    f"landings/{src.name} (no matching published+public gist: {gid})"
                )
                continue
            wrote = _mirror_file(src, target / "landings" / src.name)
            (result.landings_written if wrote else result.landings_unchanged).append(
                src.name
            )

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    # Match check_gists' Windows-console guard: degrade a stray non-ASCII byte to '?'
    # instead of crashing print on cp1252/cp437 consoles.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description=(
            "Mirror published+public gist shapes from a source gists/ dir into a "
            "receiver tree's gists/ dir (GIST-SHAPES.md §9 release-cut distribution)."
        )
    )
    parser.add_argument("source", type=Path, help="source gists/ dir (the truth tree)")
    parser.add_argument("target", type=Path, help="receiver gists/ dir to mirror into")
    parser.add_argument(
        "--public-tree",
        action="store_true",
        help="the receiver is a public tree ⇒ also mirror matching landings",
    )
    parser.add_argument(
        "--i-am-travis",
        action="store_true",
        help="bypass the bob-guard for the attended release cut into C:\\dev\\projects\\bob",
    )
    parser.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = parser.parse_args(argv)

    try:
        result = publish_gists(
            args.source,
            args.target,
            public_tree=args.public_tree,
            i_am_travis=args.i_am_travis,
        )
    except BobGuardRefusal as exc:
        print(f"REFUSED (bob-guard) {exc}")
        return 2
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"ERROR {exc}")
        return 2

    if not args.quiet:
        for name in result.gists_written:
            print(f"  copied gist     {name}")
        for name in result.gists_unchanged:
            print(f"  unchanged gist  {name}")
        for name in result.landings_written:
            print(f"  copied landing  {name}")
        for name in result.landings_unchanged:
            print(f"  unchanged land. {name}")
        for note in result.excluded:
            print(f"  excluded        {note}")

    print(result.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
