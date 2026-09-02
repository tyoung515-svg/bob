"""BoBClaw Core — verification spine §3 (MS9-F5): **measurement-entailment**.

A *longitudinal* claim (SPEC-RESEARCH-FOREST §3) entails from a **time series of typed
``measurement`` events** (the program ledger's evidence set, produced by ``core.forest.events``)
plus the hypothesis node's declared **``decision_rule``**. This module emits the SAME
``VerificationTag`` vocabulary as ``core.verify.entailment`` (``PV`` / ``VS`` / ``U``) — it is the
claim-ratchet *extended*, not a new verification class (the module manifest still declares
``verification_class: claim-ratchet``).

Two hard rules from the SPEC:

* **PURE.** No I/O. The evidence is *passed in*; there is no ledger / network / db / clock / random
  read inside the entailment. Deterministic: identical inputs → identical tag.
* **default-FAIL.** Insufficient observations ⇒ ``U``, *never* ``PV``. The §7.2 observation floor
  (``OBSERVATION_FLOOR`` = ``core.forest.hypothesis.SUPPORTED_MIN_OBSERVATIONS`` = 5) is a
  *structural gate applied BEFORE any decision-rule evaluation* — so ``PV`` is UNREACHABLE below the
  floor for every claim kind, even for evidence that WOULD entail with enough observations.

The four §7.1 claim kinds, each with a declared decision rule → tag mapping:

* ``level``            — metric *m* aggregates into a range *R* = ``[lo, hi]`` at *t*.
* ``trend``            — metric *m* moves in a declared direction over a window *w*.
* ``comparison``       — arm A vs arm B on *m*, effect δ satisfies a declared relation.
* ``causal-candidate`` — an intervention *i* moves *m* (pre-vs-post) by at least a declared amount.

Verdict → tag is delegated to ``core.verify.entailment.tag_for`` (read-only import), so the mapping
is *identical* to point-in-time entailment: ``entailed + primary → PV``, ``entailed + vendor → VS``,
anything else (``not_entailed`` / ``unknown``, incl. every below-floor case) → ``U``.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

# Read-only imports of the EXISTING verify vocabulary (never edited here).
from core.verify.entailment import (
    EntailmentVerdict,
    SourceKind,
    VerificationTag,
    tag_for,
)

# Read-only import of the F2 ratified observation floor (SPEC §7.2 = 5 observations).
from core.forest.hypothesis import (
    CLAIM_KINDS,
    ClaimKind,
    SUPPORTED_MIN_OBSERVATIONS as OBSERVATION_FLOOR,
)

# The one ledger event kind we consume as evidence (SPEC §1 / core.forest.events).
MEASUREMENT_KIND = "measurement"

__all__ = [
    "MEASUREMENT_KIND",
    "OBSERVATION_FLOOR",
    "MeasurementError",
    "MeasurementEntailment",
    "entail_measurement",
    "entail_node",
]


class MeasurementError(RuntimeError):
    """Reserved for programmer errors. The entailment surface itself is FAIL-SAFE: bad evidence or an
    unparseable rule yields an ``unknown`` verdict (tag ``U``), never a raised exception — mirroring
    the fail-safe posture of ``core.verify.entailment``."""


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MeasurementEntailment:
    """The outcome of entailing a longitudinal claim from a measurement time series."""

    verdict: EntailmentVerdict
    entailed: bool
    tag: VerificationTag
    claim_kind: str
    metric: Optional[str]
    n_observations: int
    floor: int
    floor_met: bool
    source_kind: SourceKind
    observed: Dict[str, Any] = field(default_factory=dict)
    reasons: Tuple[str, ...] = ()
    node_id: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "entailed": self.entailed,
            "tag": self.tag.value,
            "claim_kind": self.claim_kind,
            "metric": self.metric,
            "n_observations": self.n_observations,
            "floor": self.floor,
            "floor_met": self.floor_met,
            "source_kind": self.source_kind.value,
            "observed": dict(self.observed),
            "reasons": list(self.reasons),
            "node_id": self.node_id,
        }


# ── Private helpers (all pure) ───────────────────────────────────────────────

def _coerce_rule(decision_rule: Any) -> Dict[str, Any]:
    """Normalize a declared decision_rule (a dict, a JSON string, or None) to a dict.

    ``core.forest.hypothesis.HypothesisNode.decision_rule`` is a ``str | None``; a canonical form is a
    JSON object. A ``None`` / empty / unparseable rule returns ``{}`` (→ an ``unknown`` verdict upstream)
    rather than raising: the boundary never auto-passes and never crashes."""
    if decision_rule is None:
        return {}
    if isinstance(decision_rule, Mapping):
        return dict(decision_rule)
    if isinstance(decision_rule, str):
        s = decision_rule.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _claim_kind_str(claim_kind: Any) -> Optional[str]:
    if isinstance(claim_kind, ClaimKind):
        return claim_kind.value
    if isinstance(claim_kind, str):
        return claim_kind
    return None


def _is_number(v: Any) -> bool:
    # bool is an int subclass — a measurement value must be a real number, never True/False.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _measurements_for(
    events: Sequence[Mapping[str, Any]],
    node_id: Optional[str],
    metric: Optional[str],
) -> List[Mapping[str, Any]]:
    """Filter to ``measurement`` events matching the node (if given) and metric (if given)."""
    out: List[Mapping[str, Any]] = []
    for e in events:
        if not isinstance(e, Mapping):
            continue
        if e.get("kind") != MEASUREMENT_KIND:
            continue
        if node_id is not None and e.get("node_id") != node_id:
            continue
        if metric is not None and e.get("metric") != metric:
            continue
        out.append(e)
    return out


def _sorted_by_ts(events: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Order events by their caller-supplied ``ts``. If ts values are mutually uncomparable
    (mixed types), fall back to the input order — deterministic, never raises."""
    try:
        return sorted(events, key=lambda e: e.get("ts"))
    except TypeError:
        return list(events)


def _values(events: Sequence[Mapping[str, Any]], *, ordered: bool = False) -> List[float]:
    src = _sorted_by_ts(events) if ordered else events
    return [float(e["value"]) for e in src if _is_number(e.get("value"))]


def _agg(values: Sequence[float], how: str) -> float:
    if how == "mean":
        return statistics.fmean(values)
    if how == "median":
        return statistics.median(values)
    if how == "sum":
        return float(sum(values))
    if how == "min":
        return float(min(values))
    if how == "max":
        return float(max(values))
    if how == "last":
        return float(values[-1])
    if how == "first":
        return float(values[0])
    raise ValueError(f"unknown aggregation {how!r}")


def _slope(values: Sequence[float]) -> float:
    """Ordinary least-squares slope of ``values`` over their index 0..n-1 (n >= 2)."""
    n = len(values)
    xs = range(n)
    mx = (n - 1) / 2.0
    my = statistics.fmean(values)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    den = sum((x - mx) ** 2 for x in xs)
    return 0.0 if den == 0 else num / den


def _resolve_source_kind(
    rule: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    explicit: Optional[Union[SourceKind, str]],
) -> SourceKind:
    """Determine the evidence provenance (drives PV vs VS on an ``entailed`` verdict).

    Precedence: an explicit argument → the rule's ``source_kind`` → a consistent per-event
    ``source_kind`` field → default PRIMARY (measurement observations are primary evidence)."""
    def _coerce(v: Any) -> Optional[SourceKind]:
        if isinstance(v, SourceKind):
            return v
        if isinstance(v, str):
            try:
                return SourceKind(v)
            except ValueError:
                return None
        return None

    if explicit is not None:
        got = _coerce(explicit)
        if got is not None:
            return got
    got = _coerce(rule.get("source_kind"))
    if got is not None:
        return got
    seen = {e.get("source_kind") for e in events if e.get("source_kind") is not None}
    if len(seen) == 1:
        got = _coerce(next(iter(seen)))
        if got is not None:
            return got
    return SourceKind.PRIMARY


def _rel(effect: float, threshold: float, op: str, eps: float) -> bool:
    if op in (">=", "ge"):
        return effect >= threshold - eps
    if op in (">", "gt"):
        return effect > threshold + eps
    if op in ("<=", "le"):
        return effect <= threshold + eps
    if op in ("<", "lt"):
        return effect < threshold - eps
    if op in ("==", "eq", "approx", "~="):
        return abs(effect - threshold) <= eps
    if op in ("!=", "ne"):
        return abs(effect - threshold) > eps
    raise ValueError(f"unknown relation {op!r}")


# ── Per-claim-kind evaluators ────────────────────────────────────────────────
# Each returns (EntailmentVerdict, reasons, observed). They are called ONLY after the floor gate
# has passed, so an ``entailed`` here always has >= OBSERVATION_FLOOR relevant observations behind it.

def _eval_level(
    rule: Mapping[str, Any], measurements: Sequence[Mapping[str, Any]]
) -> Tuple[EntailmentVerdict, List[str], Dict[str, Any]]:
    how = rule.get("agg", "mean")
    values = _values(measurements, ordered=(how in ("last", "first")))
    if not values:
        return EntailmentVerdict.UNKNOWN, ["no numeric measurement values"], {}
    agg = _agg(values, how)
    lo = rule.get("lo", rule.get("min"))
    hi = rule.get("hi", rule.get("max"))
    if lo is None and hi is None:
        return EntailmentVerdict.UNKNOWN, ["level rule declares no range (lo/hi)"], {"agg": agg}
    observed = {"agg": agg, "how": how, "lo": lo, "hi": hi}
    ok_lo = (lo is None) or (agg >= float(lo))
    ok_hi = (hi is None) or (agg <= float(hi))
    if ok_lo and ok_hi:
        return EntailmentVerdict.ENTAILED, [f"{how}={agg} in range [{lo}, {hi}]"], observed
    return EntailmentVerdict.NOT_ENTAILED, [f"{how}={agg} outside range [{lo}, {hi}]"], observed


def _eval_trend(
    rule: Mapping[str, Any], measurements: Sequence[Mapping[str, Any]]
) -> Tuple[EntailmentVerdict, List[str], Dict[str, Any]]:
    direction = str(rule.get("direction", "")).lower()
    if direction not in ("up", "down", "flat", "increase", "decrease", "nondecreasing", "nonincreasing"):
        return EntailmentVerdict.UNKNOWN, [f"trend rule declares no/invalid direction {direction!r}"], {}
    eps = float(rule.get("eps", 0.0))
    window = rule.get("window")
    ordered = _values(measurements, ordered=True)
    if window is not None:
        w = int(window)
        ordered = ordered[-w:] if w > 0 else ordered
    if len(ordered) < 2:
        return EntailmentVerdict.UNKNOWN, ["fewer than 2 points: no trend defined"], {"points": len(ordered)}
    slope = _slope(ordered)
    observed = {"slope": slope, "points": len(ordered), "direction": direction, "eps": eps}
    if direction in ("up", "increase"):
        got = slope > eps
    elif direction in ("down", "decrease"):
        got = slope < -eps
    elif direction == "flat":
        got = abs(slope) <= eps
    elif direction == "nondecreasing":
        got = slope >= -eps
    else:  # nonincreasing
        got = slope <= eps
    if got:
        return EntailmentVerdict.ENTAILED, [f"slope={slope} matches {direction}"], observed
    return EntailmentVerdict.NOT_ENTAILED, [f"slope={slope} does not match {direction}"], observed


def _eval_comparison(
    rule: Mapping[str, Any], measurements: Sequence[Mapping[str, Any]]
) -> Tuple[EntailmentVerdict, List[str], Dict[str, Any]]:
    arm_field = rule.get("arm_field", "arm")
    arm_a = rule.get("arm_a")
    arm_b = rule.get("arm_b")
    if arm_a is None or arm_b is None:
        return EntailmentVerdict.UNKNOWN, ["comparison rule missing arm_a/arm_b"], {}
    how = rule.get("agg", "mean")
    a_vals = _values([e for e in measurements if e.get(arm_field) == arm_a], ordered=(how in ("last", "first")))
    b_vals = _values([e for e in measurements if e.get(arm_field) == arm_b], ordered=(how in ("last", "first")))
    if not a_vals or not b_vals:
        return (
            EntailmentVerdict.UNKNOWN,
            [f"an arm has no observations (A={len(a_vals)}, B={len(b_vals)})"],
            {"n_a": len(a_vals), "n_b": len(b_vals)},
        )
    effect = _agg(a_vals, how) - _agg(b_vals, how)
    delta = float(rule.get("delta", 0.0))
    op = str(rule.get("op", rule.get("direction", ">=")))
    eps = float(rule.get("eps", 0.0))
    observed = {"effect": effect, "delta": delta, "op": op, "n_a": len(a_vals), "n_b": len(b_vals)}
    if _rel(effect, delta, op, eps):
        return EntailmentVerdict.ENTAILED, [f"effect(A-B)={effect} {op} {delta}"], observed
    return EntailmentVerdict.NOT_ENTAILED, [f"effect(A-B)={effect} fails {op} {delta}"], observed


def _eval_causal(
    rule: Mapping[str, Any], measurements: Sequence[Mapping[str, Any]]
) -> Tuple[EntailmentVerdict, List[str], Dict[str, Any]]:
    how = rule.get("agg", "mean")
    split_field = rule.get("split_field")
    if split_field is not None:
        pre_key = rule.get("pre", "pre")
        post_key = rule.get("post", "post")
        pre = [e for e in measurements if e.get(split_field) == pre_key]
        post = [e for e in measurements if e.get(split_field) == post_key]
    else:
        threshold = rule.get("ts_threshold")
        if threshold is None:
            return EntailmentVerdict.UNKNOWN, ["causal rule needs split_field or ts_threshold"], {}
        ordered = _sorted_by_ts(measurements)
        try:
            pre = [e for e in ordered if e.get("ts") < threshold]
            post = [e for e in ordered if e.get("ts") >= threshold]
        except TypeError:
            return EntailmentVerdict.UNKNOWN, ["ts not comparable to ts_threshold"], {}
    pre_vals = _values(pre, ordered=(how in ("last", "first")))
    post_vals = _values(post, ordered=(how in ("last", "first")))
    if not pre_vals or not post_vals:
        return (
            EntailmentVerdict.UNKNOWN,
            [f"missing a pre/post partition (pre={len(pre_vals)}, post={len(post_vals)})"],
            {"n_pre": len(pre_vals), "n_post": len(post_vals)},
        )
    effect = _agg(post_vals, how) - _agg(pre_vals, how)
    min_effect = float(rule.get("min_effect", 0.0))
    direction = str(rule.get("direction", "increase")).lower()
    eps = float(rule.get("eps", 0.0))
    observed = {
        "effect": effect,
        "min_effect": min_effect,
        "direction": direction,
        "n_pre": len(pre_vals),
        "n_post": len(post_vals),
    }
    if direction in ("increase", "up"):
        got = effect >= min_effect - eps and effect > eps
    elif direction in ("decrease", "down"):
        got = effect <= -min_effect + eps and effect < -eps
    elif direction in ("any", "move", "change"):
        got = abs(effect) >= min_effect - eps and abs(effect) > eps
    else:
        return EntailmentVerdict.UNKNOWN, [f"invalid causal direction {direction!r}"], observed
    if got:
        return EntailmentVerdict.ENTAILED, [f"intervention moved metric by {effect} ({direction})"], observed
    return EntailmentVerdict.NOT_ENTAILED, [f"intervention effect {effect} did not meet {direction} >= {min_effect}"], observed


_EVALUATORS = {
    ClaimKind.LEVEL.value: _eval_level,
    ClaimKind.TREND.value: _eval_trend,
    ClaimKind.COMPARISON.value: _eval_comparison,
    ClaimKind.CAUSAL_CANDIDATE.value: _eval_causal,
}


# ── Public entailment surface ────────────────────────────────────────────────

def entail_measurement(
    *,
    claim_kind: Union[ClaimKind, str],
    decision_rule: Any,
    events: Sequence[Mapping[str, Any]],
    node_id: Optional[str] = None,
    source_kind: Optional[Union[SourceKind, str]] = None,
    min_observations: int = OBSERVATION_FLOOR,
) -> MeasurementEntailment:
    """Entail a longitudinal claim from a ``measurement`` time series + a declared decision rule.

    Returns a :class:`MeasurementEntailment` carrying a ``VerificationTag`` (PV / VS / U). The
    verdict→tag mapping is the SAME as point-in-time entailment (``entailment.tag_for``).

    **default-FAIL / observation floor** — the number of relevant ``measurement`` events (for
    ``node_id`` and, if the rule names one, ``metric``) is gated against ``min_observations``
    (default = §7.2 floor = 5) *before* the decision rule is ever evaluated. Below the floor the
    verdict is ``unknown`` → tag ``U``: **PV is structurally unreachable**, for every claim kind, even
    when the same evidence WOULD entail with enough observations."""
    ck = _claim_kind_str(claim_kind)
    rule = _coerce_rule(decision_rule)
    metric = rule.get("metric")
    resolved_src = _resolve_source_kind(rule, events, source_kind)

    def _result(
        verdict: EntailmentVerdict,
        reasons: List[str],
        n_obs: int,
        floor_met: bool,
        observed: Optional[Dict[str, Any]] = None,
    ) -> MeasurementEntailment:
        tag = tag_for(verdict, resolved_src)
        return MeasurementEntailment(
            verdict=verdict,
            entailed=(verdict == EntailmentVerdict.ENTAILED),
            tag=tag,
            claim_kind=ck if ck is not None else str(claim_kind),
            metric=metric,
            n_observations=n_obs,
            floor=int(min_observations),
            floor_met=floor_met,
            source_kind=resolved_src,
            observed=observed or {},
            reasons=tuple(reasons),
            node_id=node_id,
        )

    if ck not in CLAIM_KINDS:
        return _result(EntailmentVerdict.UNKNOWN, [f"unknown claim_kind {claim_kind!r}"], 0, False)

    relevant = _measurements_for(events, node_id, metric)
    n_obs = len(relevant)

    # ── default-FAIL structural gate (SPEC §7.2 floor). Applied BEFORE rule evaluation so that no
    #    claim-kind path can reach ENTAILED / PV below the floor. ────────────────────────────────
    if n_obs < int(min_observations):
        return _result(
            EntailmentVerdict.UNKNOWN,
            [
                f"observation floor not met: {n_obs} < {int(min_observations)} "
                f"observations — PV unreachable (default-FAIL: unmeasured stays U)"
            ],
            n_obs,
            False,
        )

    if not rule:
        return _result(EntailmentVerdict.UNKNOWN, ["no/empty decision_rule declared"], n_obs, True)

    evaluator = _EVALUATORS[ck]
    try:
        verdict, reasons, observed = evaluator(rule, relevant)
    except Exception as exc:  # noqa: BLE001 — fail-safe boundary: bad rule/data → unknown, never raise
        return _result(EntailmentVerdict.UNKNOWN, [f"rule evaluation error: {type(exc).__name__}: {exc}"], n_obs, True)
    return _result(verdict, reasons, n_obs, True, observed)


def entail_node(
    node: Any,
    events: Sequence[Mapping[str, Any]],
    *,
    source_kind: Optional[Union[SourceKind, str]] = None,
    min_observations: int = OBSERVATION_FLOOR,
) -> MeasurementEntailment:
    """Convenience: entail a :class:`core.forest.hypothesis.HypothesisNode` (reads its ``id``,
    ``claim_kind`` and ``decision_rule``). Read-only over the node; PURE."""
    return entail_measurement(
        claim_kind=getattr(node, "claim_kind", None),
        decision_rule=getattr(node, "decision_rule", None),
        events=events,
        node_id=getattr(node, "id", None),
        source_kind=source_kind,
        min_observations=min_observations,
    )
