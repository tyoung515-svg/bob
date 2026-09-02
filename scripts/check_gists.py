#!/usr/bin/env python3
"""check_gists.py — validator for gist-shape + landing files.

Enforces the GIST-SHAPES.md standard (`tasks/2026-07-07-gist-shapes/GIST-SHAPES.md`
§3 artifact schema, §4 lifecycle, §7 verification) and the MS-ADDENDUM §2 "G1" rule
list. It is the machine that makes "a valid gist" and "a falsifiable landing"
mechanical, so the receiving fleet cannot silently drift a transferred capability.

Design invariants (mirrors scripts/check_hygiene.py house style):
  * pyyaml is allowed (already in-stack); otherwise stdlib only.
  * default-FAIL posture (GIST-SHAPES §7 D15): an UNKNOWN frontmatter field or an
    UNKNOWN enum value is a REJECT, never a warn. Absence of a mandatory field is a
    REJECT. "Passes" means zero findings.
  * NO CWD / GLOBAL STATE — every check keys off an explicit path; the landing
    cross-check keys off an explicit `gists_dir` (default: the landing's ../ so the
    real `gists/landings/*.md` resolves its sibling `gists/<NNNN>-<slug>.md`).
  * EXIT NON-ZERO on any finding (the CLI is a guardrail; G2 runs it over the real
    `gists/` dir, and the core suite runs the two validate_* fns over fixtures).

Rule provenance is cited inline against GIST-SHAPES.md so the checker never
re-derives the standard — it enforces it.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# ── enums (GIST-SHAPES.md §3 frontmatter schema) ──────────────────────────────
CRITICALITY = {"security", "fix", "capability"}
VERIFICATION_CLASS = {"claim-ratchet", "spec-conformance", "critique", "human-taste"}
TRANSFER = {"shaped", "literal"}
STATUS = {"proposed", "shaped", "published"}
DISTRIBUTION = {"internal", "public"}
SOURCE_TREE = {"bobclaw", "bob", "enterprise"}

# Mandatory top-level frontmatter fields. `distribution` is deliberately NOT here:
# it is enum-checked when present and required-explicit only for enterprise sources
# (D17 — see check below). `facts`, `superseded-by` are legitimately optional (§3/§4).
MANDATORY_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "criticality",
    "verification_class",
    "transfer",
    "status",
    "intended",
    "provenance",
    "requires",
)
# The full legal top-level key set (§3 schema). Anything else ⇒ default-FAIL.
KNOWN_FIELDS: set[str] = set(MANDATORY_FIELDS) | {
    "distribution",
    "facts",
    "superseded-by",
    "superseded_by",
}

# provenance sub-schema (§2 IN #7 / §3): source_tree + commit mandatory; release OR
# sprint + results optional.
MANDATORY_PROVENANCE: tuple[str, ...] = ("source_tree", "commit")
KNOWN_PROVENANCE: set[str] = {"source_tree", "commit", "release", "sprint", "results"}
# requires sub-schema (§3): capabilities mandatory (capability-named); gists/baseline optional.
MANDATORY_REQUIRES: tuple[str, ...] = ("capabilities",)
KNOWN_REQUIRES: set[str] = {"capabilities", "gists", "baseline"}

# body §6-kin headings (§3): the five mandatory; Adaptation notes optional.
MANDATORY_HEADINGS: tuple[str, ...] = (
    "Intent",
    "Contract",
    "Invariants",
    "Acceptance",
    "Scope fence",
)

# D2 code-free rule (§2): a language-tagged code fence poisons the clean room.
# Data fences (yaml/json/text) are legal — field lists/schemas are data, not code.
ALLOWED_FENCE_LANGS: set[str] = {"yaml", "json", "text"}

# capability-name heuristic (§3 / addendum G1): a capability is a name, never a path.
CAP_REJECT_TOKENS: tuple[str, ...] = ("/", "\\", ".py", ".kt")

# landing verification vocabulary (§7 D15/D16).
LANDING_TAGS: set[str] = {"PV", "VS", "U"}
LANDING_STATUS: set[str] = {"landed", "landed-partial", "declined"}

# id ↔ filename agreement (§3): id is `gist-<NNNN>-<slug>`; canonical file `<NNNN>-<slug>.md`.
ID_RE = re.compile(r"^gist-(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)$")
# ATX heading line.
HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
# a code/data fence line: ``` or ~~~ (3+), optional info string.
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*(.*)$")
# a markdown table row.
TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
# a body reference to a gist id (for landings without a frontmatter `gist:` field).
GIST_REF_RE = re.compile(r"gist-\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*")


# ── finding model ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Finding:
    """One validation violation. `rule` is the GIST-SHAPES rule id it enforces."""

    rule: str
    message: str
    path: str = ""

    def render(self) -> str:
        loc = f" [{self.path}]" if self.path else ""
        return f"FAIL ({self.rule}) {self.message}{loc}"


# ── frontmatter / body split ──────────────────────────────────────────────────
def split_frontmatter(text: str) -> tuple[Optional[dict], str, Optional[str]]:
    """Return (frontmatter_dict, body, error). A gist/landing is YAML frontmatter
    fenced by `---` lines, then a §6-kin prose body. error is set (dict None) when
    the frontmatter is absent or malformed."""
    lines = text.splitlines()
    # find the opening `---` (allow a leading UTF-8 BOM / blank lines).
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines) or lines[start].strip().lstrip("﻿") != "---":
        return None, text, "missing YAML frontmatter (file must open with a '---' fence)"
    # find the closing `---`.
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text, "unterminated YAML frontmatter (no closing '---')"
    fm_text = "\n".join(lines[start + 1 : end])
    body = "\n".join(lines[end + 1 :])
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return None, body, f"invalid YAML frontmatter: {exc}"
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, body, "YAML frontmatter is not a mapping"
    return data, body, None


# ── code-fence lint (D2) ──────────────────────────────────────────────────────
def lint_code_fences(body: str, rel: str) -> list[Finding]:
    """FAIL on a language-tagged code fence whose language is not yaml/json/text.
    Untagged fences are tolerated; the rule targets *code* (the clean-room poison)."""
    findings: list[Finding] = []
    in_fence = False
    for lineno, line in enumerate(body.splitlines(), start=1):
        m = FENCE_RE.match(line)
        if not m:
            continue
        info = m.group(2).strip()
        if not in_fence:
            in_fence = True
            lang = info.split()[0].lower() if info else ""
            if lang and lang not in ALLOWED_FENCE_LANGS:
                findings.append(
                    Finding(
                        "code-fence",
                        f"language-tagged code fence '```{lang}' at body line {lineno} "
                        f"violates the D2 code-free rule (allowed: "
                        f"{'/'.join(sorted(ALLOWED_FENCE_LANGS))})",
                        rel,
                    )
                )
        else:
            in_fence = False
    return findings


# ── heading extraction ────────────────────────────────────────────────────────
def _body_headings(body: str) -> list[str]:
    return [m.group(1) for line in body.splitlines() if (m := HEADING_RE.match(line))]


def _has_heading(headings: list[str], name: str) -> bool:
    key = name.lower()
    return any(h.strip().lower().startswith(key) for h in headings)


def _count_gist_invariants(body: str) -> int:
    """Count numbered invariant items in the `## Invariants` section of a gist body."""
    headings_seen: list[int] = []
    lines = body.splitlines()
    inv_start = None
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        if inv_start is None and m.group(1).strip().lower().startswith("invariants"):
            inv_start = i
        elif inv_start is not None:
            inv_end = i
            break
    else:
        inv_end = len(lines)
    if inv_start is None:
        return 0
    count = 0
    for line in lines[inv_start + 1 : inv_end]:
        if re.match(r"^\s*\d+\.\s+\S", line):
            count += 1
    return count


# ── gist validation ───────────────────────────────────────────────────────────
def validate_gist_file(path: str | Path) -> list[Finding]:
    """Validate one gist `.md` file against GIST-SHAPES §3/§4. Empty list ⇒ valid."""
    path = Path(path)
    rel = path.name
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Finding("io", f"cannot read gist file: {exc}", rel)]

    fm, body, err = split_frontmatter(text)
    if err is not None:
        return [Finding("frontmatter", err, rel)]
    assert fm is not None

    # (1) default-FAIL: unknown top-level field.
    for key in fm:
        if key not in KNOWN_FIELDS:
            findings.append(
                Finding(
                    "unknown-field",
                    f"unknown frontmatter field '{key}' (default-FAIL; not in the "
                    f"GIST-SHAPES.md sec.3 schema)",
                    rel,
                )
            )

    # (2) mandatory fields present.
    for field in MANDATORY_FIELDS:
        if field not in fm or fm[field] is None:
            findings.append(
                Finding("missing-field", f"missing mandatory frontmatter field '{field}'", rel)
            )

    # (3) enum validity (default-FAIL on an unknown value).
    def _enum(field: str, allowed: set[str]) -> None:
        if field in fm and fm[field] is not None and fm[field] not in allowed:
            findings.append(
                Finding(
                    "bad-enum",
                    f"invalid {field} '{fm[field]}' (allowed: {'/'.join(sorted(allowed))})",
                    rel,
                )
            )

    _enum("criticality", CRITICALITY)
    _enum("verification_class", VERIFICATION_CLASS)
    _enum("transfer", TRANSFER)
    _enum("status", STATUS)
    _enum("distribution", DISTRIBUTION)

    # (4) id format + id ↔ filename agreement (§3).
    gid = fm.get("id")
    if isinstance(gid, str):
        m = ID_RE.match(gid)
        if not m:
            findings.append(
                Finding(
                    "bad-id",
                    f"id '{gid}' must match 'gist-<NNNN>-<slug>' (4-digit, lowercase slug)",
                    rel,
                )
            )
        else:
            num, slug = m.group(1), m.group(2)
            canonical = f"{num}-{slug}.md"
            example_alias = f"EXAMPLE-GIST-{num}.md"  # §10 worked-example intake name
            if path.name not in (canonical, example_alias):
                findings.append(
                    Finding(
                        "id-filename",
                        f"filename '{path.name}' does not agree with id '{gid}' "
                        f"(expected '{canonical}')",
                        rel,
                    )
                )

    # (5) provenance sub-schema + D17 (enterprise ⇒ distribution explicit).
    prov = fm.get("provenance")
    if isinstance(prov, dict):
        for key in prov:
            if key not in KNOWN_PROVENANCE:
                findings.append(
                    Finding(
                        "unknown-field",
                        f"unknown provenance sub-field '{key}' (default-FAIL)",
                        rel,
                    )
                )
        for field in MANDATORY_PROVENANCE:
            if field not in prov or prov[field] in (None, ""):
                findings.append(
                    Finding(
                        "missing-field",
                        f"missing mandatory provenance sub-field '{field}'",
                        rel,
                    )
                )
        st = prov.get("source_tree")
        if st is not None and st not in SOURCE_TREE:
            findings.append(
                Finding(
                    "bad-enum",
                    f"invalid provenance.source_tree '{st}' "
                    f"(allowed: {'/'.join(sorted(SOURCE_TREE))})",
                    rel,
                )
            )
        # D17: an enterprise-sourced gist must set `distribution` explicitly.
        if st == "enterprise" and fm.get("distribution") in (None, ""):
            findings.append(
                Finding(
                    "d17-distribution",
                    "provenance.source_tree is 'enterprise' but 'distribution' is not set "
                    "explicitly (D17: enterprise-sourced gists must declare distribution)",
                    rel,
                )
            )
    elif prov is not None:
        findings.append(Finding("frontmatter", "provenance must be a mapping", rel))

    # (6) requires sub-schema + capability-name heuristic.
    reqs = fm.get("requires")
    if isinstance(reqs, dict):
        for key in reqs:
            if key not in KNOWN_REQUIRES:
                findings.append(
                    Finding("unknown-field", f"unknown requires sub-field '{key}' (default-FAIL)", rel)
                )
        for field in MANDATORY_REQUIRES:
            if field not in reqs:
                findings.append(
                    Finding("missing-field", f"missing mandatory requires sub-field '{field}'", rel)
                )
        caps = reqs.get("capabilities")
        if caps is not None:
            if not isinstance(caps, list):
                findings.append(
                    Finding("frontmatter", "requires.capabilities must be a list", rel)
                )
            else:
                for cap in caps:
                    cap_s = str(cap)
                    bad = [t for t in CAP_REJECT_TOKENS if t in cap_s]
                    if bad:
                        findings.append(
                            Finding(
                                "capability-path",
                                f"requires.capabilities entry '{cap_s}' looks path-named "
                                f"(contains {bad!r}); capabilities are named, never path-named",
                                rel,
                            )
                        )
    elif reqs is not None:
        findings.append(Finding("frontmatter", "requires must be a mapping", rel))

    # (7) body: the five mandatory §6-kin headings.
    headings = _body_headings(body)
    for name in MANDATORY_HEADINGS:
        if not _has_heading(headings, name):
            findings.append(
                Finding("missing-heading", f"body is missing the mandatory '## {name}' heading", rel)
            )

    # (8) code-fence lint (D2).
    findings += lint_code_fences(body, rel)

    return findings


# ── landing validation ────────────────────────────────────────────────────────
def _parse_table_rows(body: str) -> Optional[list[list[str]]]:
    """Return the data rows (each a list of stripped cells) of the FIRST evidence
    table whose header names criterion/check/result/tag, or None if absent."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = TABLE_ROW_RE.match(line)
        if not m:
            continue
        header = [c.strip().lower() for c in m.group(1).split("|")]
        joined = " ".join(header)
        if ("criterion" in joined or "invariant" in joined) and "tag" in joined and (
            "result" in joined
        ):
            # header found; the next line should be the `---` separator.
            rows: list[list[str]] = []
            for row_line in lines[i + 1 :]:
                rm = TABLE_ROW_RE.match(row_line)
                if not rm:
                    break
                cells = [c.strip() for c in rm.group(1).split("|")]
                if all(set(c) <= {"-", ":", " "} for c in cells):
                    continue  # separator row
                rows.append(cells)
            return rows
    return None


def _strip_md(s: str) -> str:
    return s.replace("*", "").strip()


def validate_landing_file(
    path: str | Path, gists_dir: Optional[str | Path] = None
) -> list[Finding]:
    """Validate one landing `.md` file against GIST-SHAPES §7. gists_dir resolves the
    referenced gist for the invariant-row cross-check; default = the landing's ../
    (so a real `gists/landings/x.md` finds `gists/<NNNN>-<slug>.md`)."""
    path = Path(path)
    rel = path.name
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Finding("io", f"cannot read landing file: {exc}", rel)]

    fm, body, err = split_frontmatter(text)
    # landings MAY be frontmatter+table (recommended) OR §7-style prose; tolerate both.
    if fm is None:
        fm = {}
        body = text

    # status: frontmatter `status:` OR a body `Status:` line (markdown-bold stripped).
    status = fm.get("status")
    if status is None:
        for line in body.splitlines():
            m = re.match(r"^\s*status\s*:\s*(.+)$", line, re.IGNORECASE)
            if m:
                status = _strip_md(m.group(1)).split()[0] if m.group(1).strip() else None
                break
    if status is None:
        findings.append(Finding("landing-status", "landing has no status (need 'landed'/'landed-partial'/'declined')", rel))
    elif status not in LANDING_STATUS:
        findings.append(
            Finding(
                "landing-status",
                f"invalid landing status '{status}' "
                f"(allowed: {'/'.join(sorted(LANDING_STATUS))})",
                rel,
            )
        )

    # declined ⇒ a reason line (frontmatter `reason:` OR a body `Reason:` line).
    if status == "declined":
        has_reason = bool(str(fm.get("reason", "")).strip())
        if not has_reason:
            has_reason = any(
                re.match(r"^\s*reason\s*:\s*\S", line, re.IGNORECASE)
                for line in body.splitlines()
            )
        if not has_reason:
            findings.append(
                Finding(
                    "landing-declined-reason",
                    "declined landing must carry a one-line reason (frontmatter 'reason:' "
                    "or a body 'Reason:' line)",
                    rel,
                )
            )
        return findings  # a decline needs no evidence table or invariant rows (§4/§7)

    # landed / landed-partial: an evidence table is mandatory (default-FAIL).
    rows = _parse_table_rows(body)
    if rows is None:
        findings.append(
            Finding(
                "landing-table",
                "landing is missing the criterion->check->result->tag evidence table "
                "(GIST-SHAPES.md sec.7)",
                rel,
            )
        )
        return findings

    # every data-row tag ∈ {PV, VS, U}.
    inv_rows = 0
    for cells in rows:
        if not cells:
            continue
        first = _strip_md(cells[0])
        if re.match(r"^inv\b", first, re.IGNORECASE):
            inv_rows += 1
        tag = _strip_md(cells[-1])
        if tag not in LANDING_TAGS:
            findings.append(
                Finding(
                    "landing-tag",
                    f"evidence row tag '{tag}' not in {'/'.join(sorted(LANDING_TAGS))} "
                    f"(row: {first[:48]!r})",
                    rel,
                )
            )

    # invariant cross-check: every gist invariant needs a landing row (§7). Resolve the
    # referenced gist by its id → canonical `<NNNN>-<slug>.md` in gists_dir.
    gid = fm.get("gist")
    if not gid:
        m = GIST_REF_RE.search(text)
        gid = m.group(0) if m else None
    if not gid:
        findings.append(
            Finding(
                "landing-gist-ref",
                "landing does not reference a gist id (needed for the invariant cross-check)",
                rel,
            )
        )
        return findings

    m = ID_RE.match(str(gid))
    if not m:
        findings.append(
            Finding("landing-gist-ref", f"referenced gist id '{gid}' is malformed", rel)
        )
        return findings

    base_dir = Path(gists_dir) if gists_dir is not None else path.parent.parent
    gist_path = base_dir / f"{m.group(1)}-{m.group(2)}.md"
    if not gist_path.is_file():
        findings.append(
            Finding(
                "landing-gist-ref",
                f"referenced gist file '{gist_path.name}' not found in {base_dir} "
                f"(cannot cross-check invariants)",
                rel,
            )
        )
        return findings

    gist_body = split_frontmatter(gist_path.read_text(encoding="utf-8"))[1]
    n_inv = _count_gist_invariants(gist_body)
    if inv_rows < n_inv:
        findings.append(
            Finding(
                "landing-invariant-row",
                f"gist '{gid}' declares {n_inv} invariant(s) but the landing has only "
                f"{inv_rows} invariant (INV...) row(s); every invariant needs a row "
                f"(GIST-SHAPES.md sec.7)",
                rel,
            )
        )
    return findings


# ── directory sweep + CLI ─────────────────────────────────────────────────────
def validate_dir(gists_dir: str | Path) -> list[Finding]:
    """Validate a real `gists/` dir: every top-level `*.md` (except README.md) as a
    gist, and every `landings/*.md` as a landing."""
    gists_dir = Path(gists_dir)
    findings: list[Finding] = []
    for p in sorted(gists_dir.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        findings += validate_gist_file(p)
    landings = gists_dir / "landings"
    if landings.is_dir():
        for p in sorted(landings.glob("*.md")):
            if p.name.lower() == "readme.md":
                continue
            findings += validate_landing_file(p, gists_dir=gists_dir)
    return findings


def validate_path(path: str | Path) -> list[Finding]:
    """Dispatch a single path: a dir → validate_dir; a file under `landings/` → landing;
    otherwise → gist."""
    path = Path(path)
    if path.is_dir():
        return validate_dir(path)
    if path.parent.name == "landings":
        return validate_landing_file(path)
    return validate_gist_file(path)


def main(argv: Optional[list[str]] = None) -> int:
    # Guardrail scripts run on Windows consoles (cp1252/cp437) where a stray non-ASCII
    # byte in a message would crash `print`. Messages are ASCII by discipline; this is
    # belt-and-suspenders so a future non-ASCII message degrades to '?' instead of dying.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Validate gist-shape + landing files")
    parser.add_argument("paths", nargs="+", type=Path, help="gist/landing files or a gists/ dir")
    parser.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = parser.parse_args(argv)

    all_findings: list[Finding] = []
    for p in args.paths:
        all_findings += validate_path(p)

    if not args.quiet:
        for f in all_findings:
            print(f.render())

    if all_findings:
        print(f"\ngist check FAILED - {len(all_findings)} finding(s)")
        return 1
    print("gist check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
