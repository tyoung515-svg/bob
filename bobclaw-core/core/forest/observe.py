"""observe.py — the forest OBSERVE loop (SPEC §2) + hypothesize TRIGGER verbs (SPEC §2/§7.3).

Source resolution (fixture-driven, v0), the observer run (resolve -> dedupe -> append to the F1
program ledger), trigger evaluation (delta / falsifier / manual), and the P5 recurrence-binding
envelope. DETERMINISTIC + OFFLINE (mega-sprint invariant 13): PURE except the ledger writes done
THROUGH the injected store; the only read I/O is ``load_fixture`` (a local JSON fixture, not a live
run). Every timestamp is a caller-supplied ``ts``.

Substantive logic authored by ``deepseek_v4_flash`` from a pinned L2 contract (``author_fleet_f3.py``);
two L2 corrections applied post-authoring: (1) the ``RESOLVERS`` keys were aligned to the
underscore ``FOREST_SOURCE_KINDS`` (the worker emitted hyphenated keys that no validated source kind
could ever match), and (2) ``_emit_measurement`` was fixed to test key PRESENCE so a legitimate
``value: 0`` (a falsy-but-valid zero measurement) is no longer silently dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Dict, List, Tuple

from core.forest.events import ForestError, measurement
from core.forest.sources import ForestSource, FOREST_SOURCE_KINDS
from core.forest.hypothesis import evaluate_falsifiers
from core.watch.dedupe import fingerprint
from core.watch.registry import WatchSchedule


# ── Errors ──────────────────────────────────────────────────────────────────
class ObserveError(ForestError):
    """Raised on an unknown source kind / bad trigger verb or validation failure."""
    pass


# ── Constants ───────────────────────────────────────────────────────────────
DELTA_THRESHOLD = 10                     # SPEC §7.3: >= 10 new evidence
EVIDENCE_KINDS = frozenset({"measurement"})   # which event kinds count as evidence
OBSERVER_TASK_PREFIX = "forest.observe"  # scheduled-turn command prefix


# ── Part A — Source Resolution ──────────────────────────────────────────────

def load_fixture(path: Path) -> Any:
    """Read and parse a local JSON fixture file (utf-8). Only I/O in this module."""
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read())


def _ensure_float(value: Any) -> Optional[float]:
    """Try to convert a value to float; return None if impossible."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _readings_from_payload(payload: Any) -> List[Dict[str, Any]]:
    """Normalize payload into a list of reading dicts.

    Accepts:
        - a single dict
        - a list of dicts
        - a bare number or list of numbers (wraps into dict with no metric key)
    """
    if isinstance(payload, dict):
        # Check if it looks like a results wrapper
        if "results" in payload:
            return _readings_from_payload(payload["results"])
        return [payload]
    if isinstance(payload, (int, float)):
        return [{"value": payload}]
    if isinstance(payload, list):
        result = []
        for item in payload:
            if isinstance(item, dict):
                if "results" in item:
                    result.extend(_readings_from_payload(item["results"]))
                else:
                    result.append(item)
            elif isinstance(item, (int, float)):
                result.append({"value": item})
            else:
                # Skip non-numeric items silently
                continue
        return result
    return []


def _emit_measurement(source: ForestSource, reading: Dict[str, Any], ts: Any) -> Optional[dict]:
    """Create a measurement event from a reading dict. Returns None if value missing.

    Tests key PRESENCE (not truthiness) so a legitimate ``value: 0`` / ``0.0`` is a real reading,
    not a dropped one.
    """
    if "value" in reading:
        value_raw = reading["value"]
    elif "amount" in reading:
        value_raw = reading["amount"]
    elif "amount_usd" in reading:
        value_raw = reading["amount_usd"]
    else:
        return None
    value = _ensure_float(value_raw)
    if value is None:
        return None
    metric = reading.get("metric", source.metric)
    return measurement(
        node_id=source.node_id,
        metric=metric,
        value=value,
        source=source.id,
        ts=ts,
        unit=source.unit,
        weight=source.weight
    )


def resolve_testpipe_uplift(source: ForestSource, payload: Any, ts: Any) -> List[dict]:
    """Resolver for testpipe-uplift adapter (SPEC §5)."""
    readings = _readings_from_payload(payload)
    result = []
    for r in readings:
        ev = _emit_measurement(source, r, ts)
        if ev is not None:
            result.append(ev)
    return result


def resolve_flight_spend(source: ForestSource, payload: Any, ts: Any) -> List[dict]:
    """Resolver for flight/spend telemetry."""
    readings = _readings_from_payload(payload)
    result = []
    for r in readings:
        ev = _emit_measurement(source, r, ts)
        if ev is not None:
            result.append(ev)
    return result


def resolve_eval_results(source: ForestSource, payload: Any, ts: Any) -> List[dict]:
    """Resolver for SES / eval-results."""
    readings = _readings_from_payload(payload)
    result = []
    for r in readings:
        ev = _emit_measurement(source, r, ts)
        if ev is not None:
            result.append(ev)
    return result


def resolve_lks_stats(source: ForestSource, payload: Any, ts: Any) -> List[dict]:
    """Resolver for LKS corpus stats.

    If payload is a plain dict of {metric: numeric}, emit one measurement per key.
    Otherwise fall back to reading-dict list processing.
    """
    if isinstance(payload, dict) and not any(k in payload for k in ("results", "value", "amount")):
        # Plain key-value map
        result = []
        for metric, value_raw in payload.items():
            value = _ensure_float(value_raw)
            if value is None:
                continue
            ev = measurement(
                node_id=source.node_id,
                metric=metric,
                value=value,
                source=source.id,
                ts=ts,
                unit=source.unit,
                weight=source.weight
            )
            result.append(ev)
        return result
    # Otherwise treat as list of reading dicts
    readings = _readings_from_payload(payload)
    result = []
    for r in readings:
        ev = _emit_measurement(source, r, ts)
        if ev is not None:
            result.append(ev)
    return result


RESOLVERS: Dict[str, Callable] = {
    "testpipe_uplift": resolve_testpipe_uplift,
    "flight_spend": resolve_flight_spend,
    "eval_results": resolve_eval_results,
    "lks_stats": resolve_lks_stats,
}

# Fail loud at import time if the resolver table and the source-kind vocabulary ever drift apart
# (this is exactly the hyphen/underscore mismatch that was corrected post-authoring).
assert set(RESOLVERS) == set(FOREST_SOURCE_KINDS), (
    "RESOLVERS keys must exactly match FOREST_SOURCE_KINDS "
    f"(resolvers={sorted(RESOLVERS)}, kinds={sorted(FOREST_SOURCE_KINDS)})"
)


def resolve_source(source: ForestSource, payload: Any, ts: Any) -> List[dict]:
    """Resolve a source's payload into measurement events."""
    if source.kind not in RESOLVERS:
        raise ObserveError(f"no resolver for source kind {source.kind!r}")
    return RESOLVERS[source.kind](source, payload, ts)


# ── Part B — Running an Observer ────────────────────────────────────────────

@dataclass
class ObserveResult:
    sha: Optional[str] = None
    appended: list = field(default_factory=list)
    skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.appended)


def dedupe_events(events: List[dict], seen_ids: Iterable[str]) -> Tuple[List[dict], int]:
    """Remove duplicates by id (global + intra-batch) and by content fingerprint.

    Returns (kept_events, dropped_count).
    """
    id_set = set(seen_ids)
    fingerprint_set = set()
    kept = []
    dropped = 0
    for ev in events:
        ev_id = ev.get("id")
        if ev_id is not None and ev_id in id_set:
            dropped += 1
            continue
        # Intra-batch dedup by fingerprint of key fields
        fp = fingerprint(ev, fields=("kind", "ts", "node_id", "metric", "value", "source"))
        if fp in fingerprint_set:
            dropped += 1
            continue
        # Accept
        if ev_id is not None:
            id_set.add(ev_id)
        fingerprint_set.add(fp)
        kept.append(ev)
    return kept, dropped


def run_observer(
    store: Any,
    source: ForestSource,
    payload: Any,
    ts: Any,
    *,
    message: Optional[str] = None,
) -> ObserveResult:
    """Resolve, dedupe, and append measurements to the store's ledger."""
    events = resolve_source(source, payload, ts)
    if not events:
        return ObserveResult(sha=None, appended=[], skipped=0)

    existing_ids = {e["id"] for e in store.events()}
    kept, dropped = dedupe_events(events, existing_ids)
    if not kept:
        return ObserveResult(sha=None, appended=[], skipped=dropped)

    msg = message or f"forest: observe {source.id} ({len(kept)} measurement(s))"
    sha = store.append_events(kept, message=msg)
    return ObserveResult(sha=sha, appended=kept, skipped=dropped)


# ── Part C — Trigger Verbs ─────────────────────────────────────────────────

@dataclass
class TriggerResult:
    fired: bool
    verb: str
    count: Optional[int] = None
    reason: str = ""


def new_evidence_events(
    store: Any,
    base_ref: str,
    head: str = "HEAD",
    *,
    node_ids: Optional[set] = None,
    evidence_kinds: frozenset = EVIDENCE_KINDS,
) -> List[dict]:
    """Return new evidence-bearing events for a subtree since base_ref."""
    d = store.diff(base_ref, head)
    if not d.get("events_changed"):
        return []
    base_ids = {e["id"] for e in store.events(base_ref)}
    out = []
    for e in store.events(head):
        if e.get("id") in base_ids:
            continue
        if e.get("kind") not in evidence_kinds:
            continue
        if node_ids is not None and e.get("node_id") not in node_ids:
            continue
        out.append(e)
    return out


def count_new_evidence(
    store: Any,
    base_ref: str,
    head: str = "HEAD",
    *,
    node_ids: Optional[set] = None,
    evidence_kinds: frozenset = EVIDENCE_KINDS,
) -> int:
    """Count new evidence-bearing events since base_ref."""
    return len(new_evidence_events(
        store, base_ref, head, node_ids=node_ids, evidence_kinds=evidence_kinds
    ))


def evaluate_delta(
    store: Any,
    base_ref: str,
    head: str = "HEAD",
    *,
    node_ids: Optional[set] = None,
    threshold: int = DELTA_THRESHOLD,
    evidence_kinds: frozenset = EVIDENCE_KINDS,
) -> TriggerResult:
    """Evaluate delta trigger: fires when new evidence >= threshold."""
    n = count_new_evidence(
        store, base_ref, head, node_ids=node_ids, evidence_kinds=evidence_kinds
    )
    fired = n >= threshold
    reason = f"{n} new evidence event(s) since {base_ref} (threshold {threshold})"
    return TriggerResult(fired=fired, verb="delta", count=n, reason=reason)


def evaluate_falsifier(node: Any, evidence: Any) -> TriggerResult:
    """Evaluate falsifier trigger: fires if any falsifier predicate fires."""
    fired = evaluate_falsifiers(node, evidence)
    reason = "a falsifier predicate fired" if fired else "no falsifier fired"
    return TriggerResult(fired=fired, verb="falsifier", count=None, reason=reason)


def manual_trigger(reason: str = "manual entry point") -> TriggerResult:
    """Manual trigger always fires."""
    return TriggerResult(fired=True, verb="manual", count=None, reason=reason)


TRIGGER_DISPATCH = {
    "delta": evaluate_delta,
    "falsifier": evaluate_falsifier,
    "manual": manual_trigger,
}


def evaluate_trigger(verb: str, **kwargs) -> TriggerResult:
    """Dispatch to the appropriate trigger evaluator."""
    if verb not in TRIGGER_DISPATCH:
        raise ObserveError(f"unknown trigger verb {verb!r}")
    return TRIGGER_DISPATCH[verb](**kwargs)


# ── Part D — Recurrence Binding (P5 cron seam) ──────────────────────────────

def observer_task(program_id: str, source_id: str) -> str:
    """Compose the scheduled-turn command for an observer one-shot."""
    return f"{OBSERVER_TASK_PREFIX} program={program_id} source={source_id}"


def parse_observer_task(task: str) -> Optional[Dict[str, str]]:
    """Parse an observer task string back into program and source ids.

    Returns dict with keys 'program' and 'source' if task starts with the
    expected prefix and tokens are present; None otherwise. Missing tokens
    yield None values.
    """
    if not task or not task.startswith(OBSERVER_TASK_PREFIX):
        return None
    # Extract key=value pairs after the prefix
    remainder = task[len(OBSERVER_TASK_PREFIX):].strip()
    tokens = remainder.split()
    result: Dict[str, Optional[str]] = {"program": None, "source": None}
    for token in tokens:
        if "=" in token:
            key, _, value = token.partition("=")
            if key in result:
                result[key] = value
    return result


def observer_profile(
    name: str,
    *,
    cron: str,
    program_id: str,
    source_id: str,
    face_hint: Optional[str] = None,
    owner: Optional[str] = None,
    task: Optional[str] = None,
) -> dict:
    """Create a P5 scheduler profile envelope for a recurring observer.

    Validates the schedule via WatchSchedule. Returns a dict of shape:
        {"name": ..., "schedule": {"cron": ..., "task": ..., "face_hint": ..., "owner": ...}}
    """
    task = task or observer_task(program_id, source_id)
    # Validate using WatchSchedule (raises on invalid cron/empty task)
    try:
        _ = WatchSchedule(
            cron=cron,
            task=task,
            face_hint=face_hint,
            owner=owner,
        )
    except Exception as exc:
        raise ObserveError(
            f"Invalid observer schedule: {exc}"
        ) from exc

    return {
        "name": name,
        "schedule": {
            "cron": cron,
            "task": task,
            "face_hint": face_hint,
            "owner": owner,
        },
    }


# ── Module public API ───────────────────────────────────────────────────────
__all__ = [
    "ObserveError",
    "DELTA_THRESHOLD",
    "EVIDENCE_KINDS",
    "OBSERVER_TASK_PREFIX",
    "load_fixture",
    "resolve_testpipe_uplift",
    "resolve_flight_spend",
    "resolve_eval_results",
    "resolve_lks_stats",
    "RESOLVERS",
    "resolve_source",
    "ObserveResult",
    "dedupe_events",
    "run_observer",
    "TriggerResult",
    "new_evidence_events",
    "count_new_evidence",
    "evaluate_delta",
    "evaluate_falsifier",
    "manual_trigger",
    "evaluate_trigger",
    "observer_task",
    "parse_observer_task",
    "observer_profile",
]
