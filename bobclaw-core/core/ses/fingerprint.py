"""BoBClaw Core — SES eval harness (§2.8): trace-replay / behavioral fingerprint.

A STABLE fingerprint over a captured ledger trace's *decision points*, used so
non-determinism doesn't void regression testing: replaying a captured trace yields the
same fingerprint, while a real decision change yields a different one.

The fingerprint operates on an already-captured trace dict (the output of
``core.ledger.project.read_ledger_at`` — ``{"ref","events","claims","falsifiers"}`` — or
``core.ledger.session.ledger_slice``). It does NOT import git or the ledger; it is a pure,
deterministic transform of the dict.

What is IN vs OUT of the fingerprint:

IN (decision points):
  * each event's non-volatile body — notably ``id``, ``targets`` = [{claim, polarity}],
    and any verdict/decision fields;
  * each claim's ``id`` + non-volatile body (statement, status/verdict);
  * each falsifier's non-volatile body.
  All collections are canonicalized: sorted by the stable ``id`` decision-key and dict keys
  sorted, so a non-deterministic agent emitting the same decisions in a different wall-order
  produces the same fingerprint.

OUT (``DEFAULT_VOLATILE_KEYS`` — dropped everywhere, at every depth):
  * the trace-level ``ref`` and commit identity (ref/sha/commit/commit_sha/parent/parents/
    hash/blob/blobsha);
  * wall-clock (ts/timestamp/time/created_at/updated_at/captured_at/date/datetime);
  * run/uuid identity (uuid/session_id/conversation_id/run_id/trace_id/request_id/thread_id);
  * timing/nonce (elapsed/elapsed_ms/duration/duration_ms/wall_clock/now/nonce/seed);
  * list/dict ORDER (canonicalized by sorting on id).
  NOTE: a claim/event ``id`` is decision IDENTITY and is always KEPT — only uuid-style keys
  are volatile.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# Only UNAMBIGUOUSLY runtime/commit-identity keys belong here. Git-structural keys that could
# double as decision-bearing CLAIM/EVENT body fields (e.g. "parent" = a parent-claim relationship,
# "hash" = a content digest) are deliberately EXCLUDED — a captured ledger-DATA trace carries no
# commit-parent/blob noise, so listing them would only risk masking a real decision change.
DEFAULT_VOLATILE_KEYS: frozenset[str] = frozenset({
    # commit identity (the trace-level ref + any nested git ids)
    "ref", "sha", "commit", "commit_sha", "commits", "commit_range",
    # wall-clock / timestamps
    "ts", "timestamp", "time", "created_at", "updated_at", "captured_at", "date", "datetime",
    # run / uuid / session identity
    "uuid", "session_id", "conversation_id", "run_id", "trace_id", "request_id", "thread_id",
    # elapsed / duration / nonce
    "elapsed", "elapsed_ms", "duration", "duration_ms", "wall_clock", "now", "nonce", "seed",
})


def strip_volatile(obj: Any, volatile_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS) -> Any:
    """Return a deep-cleaned copy of *obj* with every volatile dict key removed.

    Recursively drops any dict key in *volatile_keys* at every nesting depth and inside
    lists. The key ``"id"`` is always preserved (decision identity), even if it were listed
    in *volatile_keys*. Scalars pass through. The input is never mutated.
    """
    if isinstance(obj, dict):
        return {
            k: strip_volatile(v, volatile_keys)
            for k, v in obj.items()
            if k == "id" or k not in volatile_keys
        }
    if isinstance(obj, list):
        return [strip_volatile(item, volatile_keys) for item in obj]
    return obj


def _json_key(obj: Any) -> str:
    """A stable total-order key for an arbitrary JSON-able value."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _canon_sort(obj: Any) -> Any:
    """Recursively canonicalize ORDER: sort every nested list by a stable serialization of its
    (already-canonicalized) elements, and recurse into dict values. This makes the fingerprint
    invariant to non-deterministic ordering of decision sub-collections too (e.g. an event's
    ``targets`` list), consistent with treating the top-level collections' order as noise."""
    if isinstance(obj, dict):
        return {k: _canon_sort(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return sorted((_canon_sort(x) for x in obj), key=_json_key)
    return obj


def _canonicalise(items: Any, volatile_keys: frozenset[str]) -> list[dict]:
    """Project a raw events/claims/falsifiers field into a volatile-stripped, order-canonicalized
    list of dicts sorted by ``str(id)``. Handles the three shapes the ledger produces:
      * a dict ``{id: body}``  (read_ledger_at claims) -> ``[{"id": id, **body}]``
      * a list of id strings   (ledger_slice claim ids) -> ``[{"id": id}]``
      * a list of dicts        (events / falsifiers)    -> the cleaned dicts, keyed by ``id``
    Each record's nested lists (e.g. an event's ``targets``) are canonicalized via ``_canon_sort``.
    """
    if not items:
        return []

    cleaned: list[dict] = []
    if isinstance(items, dict):
        for cid, body in items.items():
            entry = {"id": cid}
            stripped = strip_volatile(body, volatile_keys)
            if isinstance(stripped, dict):
                entry.update(stripped)
            else:
                entry["value"] = stripped
            # The ledger DICT KEY is the authoritative identity — re-assert it AFTER the body
            # merge so a divergent body "id" (corruption / version skew) can't shadow the key.
            entry["id"] = cid
            cleaned.append(_canon_sort(entry))
    elif isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                cleaned.append(_canon_sort(strip_volatile(item, volatile_keys)))
            else:
                # a bare id string (or other scalar) -> wrap as an id record
                cleaned.append({"id": item})
    else:
        return [_canon_sort(strip_volatile(items, volatile_keys))]

    return sorted(cleaned, key=lambda d: str(d.get("id", "")))


def extract_decisions(trace: dict, volatile_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS) -> dict:
    """Project a captured ledger trace down to its canonical decision surface.

    Returns ``{"events": [...], "claims": [...], "falsifiers": [...]}`` — each a
    volatile-stripped list of dicts sorted by ``id``. The trace-level ``ref`` (and any other
    top-level fields, e.g. a ``ledger_slice``'s commit-sha list / commit_range / branch) are
    NOT part of the decision surface and are dropped by construction. A ``read_ledger_at`` trace
    (events+claims+falsifiers) and a ``ledger_slice`` trace (events+claims, no falsifiers) both
    project cleanly. If *trace* is not a ledger shape (missing events or claims), falls back to
    ``strip_volatile(trace)``.
    """
    if not isinstance(trace, dict) or not ({"events", "claims"} <= trace.keys()):
        return strip_volatile(trace, volatile_keys)
    return {
        "events": _canonicalise(trace.get("events", []), volatile_keys),
        "claims": _canonicalise(trace.get("claims", {}), volatile_keys),
        "falsifiers": _canonicalise(trace.get("falsifiers", []), volatile_keys),
    }


def behavioral_fingerprint(trace: dict, *, volatile_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS) -> str:
    """Stable behavioral fingerprint for a captured ledger trace.

    ``"fp:sha256:"`` + the SHA-256 hex digest of the canonical JSON of the decision surface.
    Two calls on logically identical (but volatile-noise-differing or reordered) traces return
    the same string; a real decision change (an event target/polarity, a claim statement/status,
    an added/removed claim or event) returns a different one.
    """
    decisions = extract_decisions(trace, volatile_keys)
    payload = json.dumps(
        decisions, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")
    return f"fp:sha256:{hashlib.sha256(payload).hexdigest()}"
