"""MS9-F1 — tests for core.forest.events (the additive forest event kinds).

Pins: all 9 kinds constructible + valid + carry a non-empty str id; PURE/deterministic ids
(content-addressed); the validator's fail-loud rules (bad kind, missing ts, missing required field,
bool-as-numeric, non-str id); REQUIRED_FIELDS ⇔ EVENT_KINDS coherence.
"""
from __future__ import annotations

import pytest

from core.forest import events as fe
from core.forest.events import (
    EVENT_KINDS,
    REQUIRED_FIELDS,
    ForestError,
    ForestEventError,
    is_forest_event,
    validate_event,
)

# One well-formed call per kind (keyword args), ts supplied by the caller (no clock).
_CONSTRUCTORS = {
    "measurement": lambda: fe.measurement(node_id="H1", metric="uplift", value=0.4, source="testpipe", ts=1000),
    "spend": lambda: fe.spend(amount_usd=0.03, label="observer tick", ts=1000),
    "epoch_open": lambda: fe.epoch_open(epoch_id="E1", trigger="delta", ts=1000),
    "epoch_close": lambda: fe.epoch_close(epoch_id="E1", ts=1001),
    "fork_proposed": lambda: fe.fork_proposed(fork_id="F1", parent_program="p", rationale="outgrew", ts=1000),
    "fork_approved": lambda: fe.fork_approved(fork_id="F1", child_program="c", ts=1000),
    "pull_from_parent": lambda: fe.pull_from_parent(parent_program="p", from_ref="deadbeef", ts=1000),
    "experiment_run": lambda: fe.experiment_run(experiment_id="X1", node_id="H1", runner="testpipe", ts=1000),
    "archive_proposed": lambda: fe.archive_proposed(program_id="p", rationale="all dormant", ts=1000),
}


def test_event_kinds_are_exactly_the_nine():
    assert EVENT_KINDS == {
        "measurement", "spend", "epoch_open", "epoch_close", "fork_proposed",
        "fork_approved", "pull_from_parent", "experiment_run", "archive_proposed",
    }
    # REQUIRED_FIELDS declares required fields for every kind and no extras.
    assert set(REQUIRED_FIELDS) == set(EVENT_KINDS)


@pytest.mark.parametrize("kind", sorted(_CONSTRUCTORS))
def test_every_constructor_makes_a_valid_event(kind):
    ev = _CONSTRUCTORS[kind]()
    assert ev["kind"] == kind
    assert ev["ts"] == 1000 or ev["ts"] == 1001
    assert isinstance(ev["id"], str) and ev["id"].startswith("evt:") and len(ev["id"]) > 4
    validate_event(ev)  # must not raise
    assert is_forest_event(ev) is True
    # every declared required field is present and non-None
    for name in REQUIRED_FIELDS[kind]:
        assert ev.get(name) is not None


def test_measurement_defaults_and_dropped_optionals():
    ev = fe.measurement(node_id="H1", metric="uplift", value=0.4, source="testpipe", ts=1000)
    assert ev["weight"] == 1          # default carried onto the line
    assert "unit" not in ev           # None optional dropped
    ev2 = fe.measurement(node_id="H1", metric="uplift", value=0.4, source="testpipe", ts=1000, unit="ratio", weight=2)
    assert ev2["unit"] == "ratio" and ev2["weight"] == 2


def test_spend_is_est_badged_by_default():
    ev = fe.spend(amount_usd=1.5, label="epoch", ts=1000)
    assert ev["est"] is True          # EST framing mandatory (COST-2); False rides the line too
    assert fe.spend(amount_usd=1.5, label="epoch", ts=1000, est=False)["est"] is False


def test_ids_are_deterministic_and_content_addressed():
    a = fe.measurement(node_id="H1", metric="uplift", value=0.4, source="testpipe", ts=1000)
    b = fe.measurement(node_id="H1", metric="uplift", value=0.4, source="testpipe", ts=1000)
    assert a["id"] == b["id"]                       # identical inputs -> identical id (deterministic)
    c = fe.measurement(node_id="H1", metric="uplift", value=0.5, source="testpipe", ts=1000)
    assert c["id"] != a["id"]                       # a different value -> a different id


def test_explicit_event_id_used_verbatim_and_empty_rejected():
    ev = fe.epoch_open(epoch_id="E1", trigger="manual", ts=1000, event_id="evt:custom-1")
    assert ev["id"] == "evt:custom-1"
    with pytest.raises(ForestEventError):
        fe.epoch_open(epoch_id="E1", trigger="manual", ts=1000, event_id="")


def test_extra_kwargs_pass_through():
    ev = fe.measurement(node_id="H1", metric="uplift", value=0.4, source="testpipe", ts=1000, arm="A")
    assert ev["arm"] == "A"


def test_forest_error_is_base_of_event_error():
    assert issubclass(ForestEventError, ForestError)


# ---- validator fail-loud rules ----

def test_validate_rejects_non_dict():
    with pytest.raises(ForestEventError):
        validate_event(["not", "a", "dict"])


def test_validate_rejects_bad_or_missing_kind():
    with pytest.raises(ForestEventError):
        validate_event({"kind": "not_a_kind", "ts": 1})
    with pytest.raises(ForestEventError):
        validate_event({"ts": 1, "node_id": "x"})


def test_validate_rejects_missing_ts():
    with pytest.raises(ForestEventError):
        validate_event({"kind": "epoch_close", "epoch_id": "E1"})  # no ts
    with pytest.raises(ForestEventError):
        validate_event({"kind": "epoch_close", "epoch_id": "E1", "ts": None})


def test_validate_rejects_missing_required_field():
    with pytest.raises(ForestEventError):
        # measurement missing 'source'
        validate_event({"kind": "measurement", "ts": 1, "node_id": "H1", "metric": "m", "value": 1.0})


def test_validate_rejects_bool_as_numeric():
    # bool is an int subclass — a measurement value / spend amount must not be True/False.
    with pytest.raises(ForestEventError):
        validate_event({"kind": "measurement", "ts": 1, "node_id": "H1", "metric": "m", "value": True, "source": "s"})
    with pytest.raises(ForestEventError):
        validate_event({"kind": "spend", "ts": 1, "amount_usd": False, "label": "x"})


def test_constructor_rejects_bool_value():
    with pytest.raises(ForestEventError):
        fe.measurement(node_id="H1", metric="m", value=True, source="s", ts=1)


def test_validate_rejects_nonstring_or_empty_id():
    with pytest.raises(ForestEventError):
        validate_event({"kind": "epoch_close", "ts": 1, "epoch_id": "E1", "id": 123})
    with pytest.raises(ForestEventError):
        validate_event({"kind": "epoch_close", "ts": 1, "epoch_id": "E1", "id": ""})


def test_validate_accepts_idless_payload():
    # the builder validates before stamping the id — a valid id-less payload must pass.
    validate_event({"kind": "epoch_close", "ts": 1, "epoch_id": "E1"})


def test_is_forest_event_false_paths():
    assert is_forest_event({"kind": "nope"}) is False
    assert is_forest_event("x") is False
    assert is_forest_event({}) is False
