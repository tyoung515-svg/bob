from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.ledger.gitdag import _git, GitError


def blame_claim(repo, claim_id, *, events_path="ledger/events.jsonl") -> list[dict]:
    """
    Parse git blame --line-porcelain for events_path and return provenance entries
    for events whose targets include the given claim_id.
    """
    # Option-injection guard: events_path is a positional after `--`.
    if events_path.startswith("-"):
        raise GitError(f"Path must not start with '-': {events_path}")

    result = _git(repo, "blame", "--line-porcelain", "--", events_path)
    lines = result.stdout.splitlines()

    provenance: list[dict] = []
    current_sha: str | None = None
    current_author: str | None = None
    current_author_time: str | None = None

    for line in lines:
        # Detect start of a new blame group (40-hex sha)
        if re.match(r'^[0-9a-f]{40} ', line):
            current_sha = line.split(' ', 1)[0]
            current_author = None
            current_author_time = None
            continue

        if line.startswith("author "):
            current_author = line[7:].strip()
            continue

        if line.startswith("author-time "):
            current_author_time = line[12:].strip()
            continue

        if line.startswith("\t"):
            content = line[1:]  # remove leading tab
            try:
                event = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if "id" not in event:
                continue
            targets = event.get("targets", [])
            if not isinstance(targets, list):
                continue

            # Check if any target has claim matching claim_id
            matched = any(
                isinstance(t, dict) and t.get("claim") == claim_id
                for t in targets
            )
            if matched:
                # --line-porcelain repeats the sha + author-time for EVERY line, so these are
                # always set on well-formed output. Guard defensively: a truncated/corrupt blame
                # (content before its headers) skips the line instead of crashing on int(None) or
                # recording commit=None. Cannot drop a real event (the headers always precede the
                # content line in porcelain).
                if not current_sha or not current_author_time:
                    continue
                epoch = int(current_author_time)
                date_str = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                statement = (event.get("statement", "") or "")[:200]
                provenance.append({
                    "event_id": event["id"],
                    "commit": current_sha,
                    "author": current_author or "",
                    "date": date_str,
                    "statement": statement,
                })

    return provenance


def render_decision_log(repo, *, ref="HEAD", paths=None, output_rel="decision-log/experiment-log.md", write=True) -> str:
    """
    Render the commit DAG as decision log markdown.
    Guard ref and paths against leading-dash injection.
    Uses git log with record/field separators. Returns markdown string,
    optionally writes to repo/output_rel.
    """
    # Option-injection guard
    if ref.startswith("-"):
        raise GitError(f"Ref must not start with '-': {ref}")

    log_args = [
        "log", ref,
        "-z",
        "--date=short",
        "--format=%H%x00%an%x00%ad%x00%s%x00%b"
    ]

    if paths:
        for p in paths:
            if p.startswith("-"):
                raise GitError(f"Path must not start with '-': {p}")
        log_args.append("--")
        log_args.extend(paths)

    result = _git(repo, *log_args)
    output = result.stdout

    # With -z, git emits a flat NUL-separated stream:
    #   sha, author, date, subject, body, sha, author, ...
    # NUL cannot appear in valid git headers (author/date/sha) and is the
    # standard safe delimiter for arbitrary commit messages, avoiding the
    # ambiguity of \x1f/\x1e when those bytes occur in the commit body.
    fields = output.split("\x00") if output else []
    if fields and fields[-1] == "":
        fields.pop()

    blocks = ["# Decision log (derived from the ledger commit DAG)"]

    if not fields:
        blocks.append("_(no commits)_")
    else:
        for i in range(0, len(fields), 5):
            chunk = fields[i:i + 5]
            if len(chunk) < 5:
                continue
            sha, author, date, subject, body = chunk
            short_sha = sha[:7] if len(sha) >= 7 else sha
            body = body.strip() if body else ""
            commit_lines = [f"## {short_sha} — {date} — {subject}", "", author]
            if body:
                commit_lines += ["", body]
            blocks.append("\n".join(commit_lines))

    markdown = "\n\n".join(blocks) + "\n"

    if write:
        out_path = Path(repo) / output_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")

    return markdown
