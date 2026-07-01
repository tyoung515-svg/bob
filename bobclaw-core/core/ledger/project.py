from __future__ import annotations

import hashlib
import json
import pathlib

from core.ledger.gitdag import _git, GitError


def _guard(value: str) -> None:
    """Fail-closed: reject an empty/blank positional (an empty `ledger_dir` makes
    `git ls-tree -- ""` scan the whole repo), one that could be misread as a git flag (leading
    '-'), or one that smuggles a path-traversal / commit-range (`..`). No legitimate single ref or
    ledger_dir needs any of these, so this never rejects a valid input."""
    if not value or not value.strip():
        raise GitError("empty/blank argument not allowed")
    if value.startswith('-'):
        raise GitError(f"Offending option-like argument: {value!r}")
    if ".." in value:
        raise GitError(f"'..' not allowed (path-traversal / ref-range): {value!r}")


def _norm_dir(ledger_dir: str) -> str:
    """Normalize a ledger_dir so format variants agree ("ledger/" / "./ledger" -> "ledger"). Keeps
    the projection key content-addressed (input-format-independent) and avoids `ledger//events.jsonl`
    / a `./`-prefixed pathspec. (`..` is rejected by _guard, so no traversal slips through here.)"""
    d = ledger_dir.strip()
    while d.startswith("./"):
        d = d[2:]
    return d.rstrip("/")


def read_ledger_at(repo, ref="HEAD", *, ledger_dir="ledger") -> dict:
    """Load the ledger TRUTH as of a commit, reading git blobs only.

    Returns a dict with keys: ref (full SHA), events (list), claims (dict id->claim),
    falsifiers (list). Missing files yield empty lists, never crash.
    """
    _guard(ref)
    ledger_dir = _norm_dir(ledger_dir)
    _guard(ledger_dir)

    resolved = _git(repo, "rev-parse", ref).stdout.strip()

    # ---- events ----
    events = []
    events_path = f"{ledger_dir}/events.jsonl"
    result = _git(repo, "show", f"{ref}:{events_path}", allow_fail=True)
    if result.returncode == 0:  # file exists
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "id" in obj:
                events.append(obj)

    # ---- falsifiers ----
    falsifiers = []
    fals_path = f"{ledger_dir}/falsifiers.jsonl"
    result = _git(repo, "show", f"{ref}:{fals_path}", allow_fail=True)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "id" in obj:
                falsifiers.append(obj)

    # ---- claims ----
    claims_dir = f"{ledger_dir}/claims"
    # -z -> NUL-separated, UNQUOTED paths (robust to core.quotePath / special chars).
    result = _git(repo, "ls-tree", "-r", "--name-only", "-z", ref, "--", claims_dir, allow_fail=True)
    claim_paths = []
    if result.returncode == 0:
        claim_paths = [p for p in result.stdout.split("\0") if p]
    claims = {}
    for path in claim_paths:
        if not path.endswith(".json"):
            continue
        blob = _git(repo, "show", f"{ref}:{path}").stdout
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Key by the claim's "id" ONLY when it is a non-empty string; otherwise fall back to the
        # filename stem (a stable str). Guards against "id": null / non-string id corrupting the
        # dict with a None/int key (obj.get(..., default) would NOT catch an explicit null).
        claim_id = obj.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            claim_id = pathlib.PurePosixPath(path).stem
        claims[claim_id] = obj

    return {
        "ref": resolved,
        "events": events,
        "claims": claims,
        "falsifiers": falsifiers,
    }


def projection_key(repo, ref="HEAD", *, ledger_dir="ledger") -> str:
    """Content-addressed projection key, COMMIT-INDEPENDENT.

    Two commits with byte-identical ``ledger_dir`` trees produce the same key.
    Prefix ``proj:sha256:``, then SHA-256 digest of sorted (path, blobsha) list.
    """
    _guard(ref)
    ledger_dir = _norm_dir(ledger_dir)
    _guard(ledger_dir)

    # Resolve the ref FIRST (not allow_fail) so a bad/typo ref RAISES, matching read_ledger_at —
    # otherwise the allow_fail ls-tree below would silently return the empty-tree key and mask it.
    _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")

    # -z -> records are NUL-separated and paths are UNQUOTED, so the key is deterministic regardless
    # of core.quotePath / paths with spaces or special chars. Each record: "<mode> <type> <blob>\t<path>".
    result = _git(repo, "ls-tree", "-r", "-z", ref, "--", ledger_dir, allow_fail=True)
    entries = []
    if result.returncode == 0:
        for record in result.stdout.split("\0"):
            if not record:
                continue
            meta, _tab, path = record.partition("\t")
            if not path:
                continue
            meta_parts = meta.split(" ")
            if len(meta_parts) < 3:
                continue
            blobsha = meta_parts[2]
            entries.append([path, blobsha])
    entries.sort(key=lambda x: x[0])  # sort by path

    payload = json.dumps(
        {"ledger_dir": ledger_dir, "blobs": entries},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"proj:sha256:{digest}"


def diff_ledger(repo, base, head, *, ledger_dir="ledger") -> dict:
    """Incremental change-set between two commits inside ``ledger_dir``.

    Returns dict with keys: added, modified, deleted (lists of paths),
    claims_changed (sorted unique claim ids), events_changed, falsifiers_changed (bool).
    """
    _guard(base)
    _guard(head)
    ledger_dir = _norm_dir(ledger_dir)
    _guard(ledger_dir)

    # -z -> a flat NUL-separated token stream with UNQUOTED paths: for A/M/D it is
    # "<status>\0<path>", for renames/copies "R<score>\0<old>\0<new>" / "C<score>\0<old>\0<new>".
    result = _git(repo, "diff", "--name-status", "-z", base, head, "--", ledger_dir)
    tokens = [t for t in result.stdout.split("\0") if t != ""]

    added = []
    modified = []
    deleted = []

    i = 0
    n = len(tokens)
    while i < n:
        status = tokens[i]
        i += 1
        if status.startswith("R") or status.startswith("C"):
            # rename/copy carries TWO paths (old, new)
            if i + 1 >= n:
                break
            old_path, new_path = tokens[i], tokens[i + 1]
            i += 2
            added.append(new_path)
            if status.startswith("R"):
                deleted.append(old_path)  # a copy leaves the source in place
        else:
            if i >= n:
                break
            path = tokens[i]
            i += 1
            if status == "A":
                added.append(path)
            elif status == "D":
                deleted.append(path)
            else:
                # M (and any other single-path status, e.g. T type-change) -> modified
                modified.append(path)

    # Collect claim ids from any changed path under claims/
    claims_dir_prefix = f"{ledger_dir}/claims/"
    claims_changed = set()
    for path in added + modified + deleted:
        if path.startswith(claims_dir_prefix) and path.endswith(".json"):
            stem = pathlib.PurePosixPath(path).stem
            claims_changed.add(stem)

    events_file = f"{ledger_dir}/events.jsonl"
    falsifiers_file = f"{ledger_dir}/falsifiers.jsonl"

    all_changed = set(added + modified + deleted)
    events_changed = events_file in all_changed
    falsifiers_changed = falsifiers_file in all_changed

    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "claims_changed": sorted(claims_changed),
        "events_changed": events_changed,
        "falsifiers_changed": falsifiers_changed,
    }
