"""MS#4 · P1 — Slice 4 tests (centroid coarse-router + F-2 routing eval).

PURE. Routing decides proceed-vs-escalate with provenance; the F-2 eval is a blocking check over
planted within-notebook (shouldn't escalate) and cross/edge (should escalate) cases.
"""
from core.surface import (
    cosine,
    decode_centroid,
    encode_centroid,
    route_query,
    routing_eval,
)

# two well-separated notebook centroids (orthogonal-ish)
_CENTROIDS = {"nb-A": [1.0, 0.0, 0.0], "nb-B": [0.0, 1.0, 0.0]}


def test_cosine_and_centroid_codec():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([], [1]) == 0.0                          # fail-safe, never raises
    assert decode_centroid(encode_centroid([0.5, 0.25])) == [0.5, 0.25]
    assert decode_centroid(None) is None


def test_route_grounded_when_clearly_in_one_notebook():
    d = route_query([0.98, 0.02, 0.0], _CENTROIDS)
    assert d.notebook_id == "nb-A" and d.escalate is False
    assert d.provenance["reason"] == "grounded" and d.provenance["top3"][0][0] == "nb-A"


def test_route_escalates_when_far_from_all():
    d = route_query([0.0, 0.0, 1.0], _CENTROIDS)           # orthogonal to both → low similarity
    assert d.escalate is True and d.provenance["reason"] == "far_from_all"


def test_route_escalates_when_ambiguous_span():
    d = route_query([1.0, 1.0, 0.0], _CENTROIDS)           # equidistant to A and B → tiny margin
    assert d.escalate is True and d.provenance["reason"] == "ambiguous_span"


def test_route_no_centroids_escalates():
    d = route_query([1.0, 0.0], {})
    assert d.escalate is True and d.notebook_id is None and d.provenance["reason"] == "no_centroids"


# ── F-2 blocking routing eval ────────────────────────────────────────────────

def test_routing_eval_passes_on_separable_cases():
    cases = [
        {"query_vec": [0.99, 0.01, 0.0], "should_escalate": False},   # clearly A
        {"query_vec": [0.01, 0.99, 0.0], "should_escalate": False},   # clearly B
        {"query_vec": [0.0, 0.0, 1.0], "should_escalate": True},      # far
        {"query_vec": [1.0, 1.0, 0.0], "should_escalate": True},      # ambiguous
    ]
    r = routing_eval(cases, _CENTROIDS)
    assert r["missed_escalation_rate"] == 0.0 and r["passed"] is True


def test_routing_eval_flags_missed_escalation():
    # a cross-notebook query mislabeled-expectation surfaces as a missed escalation → block
    cases = [{"query_vec": [0.99, 0.01, 0.0], "should_escalate": True}]  # actually grounded → miss
    r = routing_eval(cases, _CENTROIDS)
    assert r["missed_escalation_rate"] == 1.0 and r["passed"] is False


def test_routing_eval_empty_is_not_vacuously_green():
    assert routing_eval([], _CENTROIDS)["passed"] is False
