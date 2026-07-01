import pytest
from core.ledger.commits import canonical_commit, commit_hash, should_commit, squash_trajectory
from core.ledger.types import BoundaryKind


def test_canonical_commit_keys_order_independent():
    """Different key orderings yield identical canonical dicts."""
    record1 = {"trajectory_id": "t1", "parents": ["a", "b"], "boundary_kind": "MERGE",
               "message": "hello", "claims": ["c1", "c2"]}
    record2 = {"claims": ["c2", "c1"], "parents": ["b", "a"], "trajectory_id": "t1",
               "message": "hello", "boundary_kind": "MERGE"}
    assert canonical_commit(record1) == canonical_commit(record2)


def test_canonical_commit_ignores_extra_keys():
    """Extra keys are stripped; output contains only the defined keys."""
    record = {"trajectory_id": "t1", "parents": ["p"], "boundary_kind": "BRANCH_START",
              "message": "msg", "claims": ["cl"], "extra_key": "ignored", "another": 42}
    result = canonical_commit(record)
    expected_keys = {"trajectory_id", "parents", "boundary_kind", "message", "claims"}
    assert set(result.keys()) == expected_keys


def test_canonical_commit_trajectory_id_missing_defaults_to_empty():
    """Missing or None trajectory_id becomes empty string."""
    assert canonical_commit({})["trajectory_id"] == ""
    assert canonical_commit({"trajectory_id": None})["trajectory_id"] == ""


def test_canonical_commit_trajectory_id_str_converted():
    """Non-string trajectory_id is converted via str()."""
    assert canonical_commit({"trajectory_id": 123})["trajectory_id"] == "123"


def test_canonical_commit_parents_default_empty():
    """Missing or None parents become empty list."""
    assert canonical_commit({})["parents"] == []
    assert canonical_commit({"parents": None})["parents"] == []


def test_canonical_commit_parents_dedup_and_sort():
    """Duplicate and unsorted parents are sorted and deduplicated."""
    result = canonical_commit({"parents": ["z", "a", "a", "m"]})
    assert result["parents"] == ["a", "m", "z"]


def test_canonical_commit_parents_preserves_empty_list():
    """Empty list is kept as empty."""
    assert canonical_commit({"parents": []})["parents"] == []


def test_canonical_commit_boundary_kind_default_empty():
    """Missing boundary_kind becomes empty string."""
    assert canonical_commit({})["boundary_kind"] == ""


def test_canonical_commit_message_strips_collapses_whitespace():
    """Leading/trailing whitespace stripped; internal runs collapsed to single space."""
    result = canonical_commit({"message": "  hello   world  "})
    assert result["message"] == "hello world"


def test_canonical_commit_message_empty_if_missing():
    """Missing message becomes empty string (after splitting empty string)."""
    assert canonical_commit({})["message"] == ""


def test_canonical_commit_message_non_string_converted_to_str():
    """Non-string message is converted via str() before whitespace processing."""
    result = canonical_commit({"message": 42})
    assert result["message"] == "42"


def test_canonical_commit_claims_default_empty():
    """Missing or None claims become empty list."""
    assert canonical_commit({})["claims"] == []
    assert canonical_commit({"claims": None})["claims"] == []


def test_canonical_commit_claims_dedup_and_sort():
    """Claims are sorted, deduplicated, and converted to strings."""
    result = canonical_commit({"claims": [2, "1", 2, "3"]})
    assert result["claims"] == ["1", "2", "3"]


def test_canonical_commit_claims_empty_list():
    """Explicit empty claims list kept as empty."""
    assert canonical_commit({"claims": []})["claims"] == []


def test_commit_hash_consistency():
    """Same record yields same hash every time."""
    record = {"trajectory_id": "t1", "parents": ["a"], "boundary_kind": "MERGE",
              "message": "msg", "claims": ["c1"]}
    h1 = commit_hash(record)
    h2 = commit_hash(record)
    assert h1 == h2


def test_commit_hash_different_content():
    """Different records produce different hashes (with high probability)."""
    rec_a = {"trajectory_id": "t1", "parents": [], "boundary_kind": "MERGE",
             "message": "a", "claims": []}
    rec_b = {"trajectory_id": "t2", "parents": [], "boundary_kind": "MERGE",
             "message": "a", "claims": []}
    assert commit_hash(rec_a) != commit_hash(rec_b)


def test_commit_hash_order_independent():
    """Same logical record with different key ordering yields identical hash."""
    rec1 = {"trajectory_id": "t1", "parents": ["x", "y"], "boundary_kind": "TOOL_CALL",
            "message": "  hi ", "claims": ["c1"]}
    rec2 = {"message": "  hi ", "claims": ["c1"], "boundary_kind": "TOOL_CALL",
            "trajectory_id": "t1", "parents": ["y", "x"]}
    assert commit_hash(rec1) == commit_hash(rec2)


def test_should_commit_tool_call_false():
    """should_commit returns False for BoundaryKind.TOOL_CALL."""
    assert should_commit(BoundaryKind.TOOL_CALL.value) is False


def test_should_commit_artifact_complete_true():
    """should_commit returns True for BoundaryKind.ARTIFACT_COMPLETE."""
    assert should_commit(BoundaryKind.ARTIFACT_COMPLETE.value) is True


def test_should_commit_branch_start_true():
    """should_commit returns True for BoundaryKind.BRANCH_START."""
    assert should_commit(BoundaryKind.BRANCH_START.value) is True


def test_should_commit_merge_true():
    """should_commit returns True for BoundaryKind.MERGE."""
    assert should_commit(BoundaryKind.MERGE.value) is True


def test_should_commit_correction_true():
    """should_commit returns True for BoundaryKind.CORRECTION."""
    assert should_commit(BoundaryKind.CORRECTION.value) is True


def test_should_commit_unknown_string_returns_true():
    """For any string not equal to TOOL_CALL, should_commit returns True (simplest rule)."""
    assert should_commit("UNKNOWN") is True
    assert should_commit("") is True
    assert should_commit("TOOL_CALL") is False  # exact match still False


def test_squash_trajectory_basic():
    """Basic two-step squash returns correct dict with all keys."""
    steps = [
        {"message": "first", "claims": ["c1", "c2"], "parents": ["p1"]},
        {"message": "second", "claims": ["c3"], "parents": ["p2"]}
    ]
    result = squash_trajectory(steps, trajectory_id="tid", boundary_kind="MERGE")
    assert result["trajectory_id"] == "tid"
    # parents from first step
    assert result["parents"] == ["p1"]
    assert result["boundary_kind"] == "MERGE"
    # message concatenated in order
    assert result["message"] == "first second"
    # claims union, order of first occurrence
    assert result["claims"] == ["c1", "c2", "c3"]


def test_squash_trajectory_claims_union_preserves_first_seen_order():
    """Claims are deduplicated preserving the order of first occurrence."""
    steps = [
        {"message": "a", "claims": ["x", "y"]},
        {"message": "b", "claims": ["y", "z"]},
        {"message": "c", "claims": ["x", "z", "w"]}
    ]
    result = squash_trajectory(steps, trajectory_id="t")
    assert result["claims"] == ["x", "y", "z", "w"]


def test_squash_trajectory_message_from_steps_with_message():
    """Only steps that contain a 'message' key contribute to the final message."""
    steps = [
        {"message": "first", "claims": ["c"]},
        {"no_message": True, "claims": ["d"]},
        {"message": "third", "claims": ["e"]}
    ]
    result = squash_trajectory(steps, trajectory_id="t")
    assert result["message"] == "first third"


def test_squash_trajectory_parents_from_first_step_ignores_later():
    """Only the first step's parents are used, even if later steps have them."""
    steps = [
        {"message": "a", "parents": ["p1"]},
        {"message": "b", "parents": ["p2"]},
        {"message": "c", "parents": []}
    ]
    result = squash_trajectory(steps, trajectory_id="t")
    assert result["parents"] == ["p1"]


def test_squash_trajectory_idempotent_single_step():
    """Squashing a single step yields the same record as canonical_commit of that step."""
    step = {"message": "single", "claims": ["c"], "parents": ["p"],
            "trajectory_id": "tid", "boundary_kind": "ARTIFACT_COMPLETE"}
    # squash_trajectory ignores trajectory_id and boundary_kind from step (they are params)
    result = squash_trajectory([step], trajectory_id="tid", boundary_kind="ARTIFACT_COMPLETE")
    # The step itself already has the expected keys; squash should produce equivalent canonical form.
    # Use canonical_commit to compare (which normalizes)
    expected = canonical_commit(step)
    # The expected canonical form might differ if step had extra keys, but we assume clean.
    # However, squash_trajectory sets its own trajectory_id, parents, etc. For single step,
    # parents from step's parents, message from step, claims from step.
    # So result should match canonical_commit(step) exactly.
    assert canonical_commit(result) == canonical_commit(step)


def test_squash_trajectory_custom_boundary_kind():
    """The boundary_kind argument overrides any boundary_kind from steps."""
    step = {"message": "x", "claims": [], "parents": []}
    result = squash_trajectory([step], trajectory_id="t", boundary_kind="CORRECTION")
    assert result["boundary_kind"] == "CORRECTION"


def test_squash_trajectory_claims_empty_when_no_claims_in_steps():
    """If no step has a 'claims' key, the result's claims is empty list."""
    steps = [
        {"message": "a", "parents": []},
        {"message": "b", "parents": []}
    ]
    result = squash_trajectory(steps, trajectory_id="t")
    assert result["claims"] == []


def test_squash_trajectory_message_empty_when_no_step_has_message():
    """If no step has a 'message' key, the result's message is empty string."""
    steps = [
        {"parents": ["p1"]},
        {"parents": ["p2"]}
    ]
    result = squash_trajectory(steps, trajectory_id="t")
    assert result["message"] == ""


def test_commit_hash_uses_canonical_commit():
    """commit_hash is deterministic and corresponds to canonical_commit output."""
    record = {"trajectory_id": "t", "parents": ["a", "b"], "boundary_kind": "MERGE",
              "message": "  test  ", "claims": ["c2", "c1"]}
    canonical = canonical_commit(record)
    # The hash should be independent of the original order
    h1 = commit_hash(record)
    h2 = commit_hash(canonical)
    assert h1 == h2
