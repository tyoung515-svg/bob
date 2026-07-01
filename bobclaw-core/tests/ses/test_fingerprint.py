from __future__ import annotations

import json
from pathlib import Path
from copy import deepcopy
from hashlib import sha256

import pytest

from core.ses.fingerprint import (
    strip_volatile,
    extract_decisions,
    behavioral_fingerprint,
    DEFAULT_VOLATILE_KEYS,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ledger_trace.json"


def _load_trace() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _naive_hash(obj: object) -> str:
    return sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    ).hexdigest()


def _add_noise(trace: dict) -> dict:
    """Return a deep copy with volatile noise added and ordering scrambled."""
    noisy = deepcopy(trace)
    # Change ref
    noisy["ref"] = "deadbeef1234"
    # Add volatile keys at top level
    noisy["captured_at"] = "2026-06-29T12:00:00Z"
    noisy["uuid"] = "abc-123"
    noisy["session_id"] = "sess_42"
    # Add timestamps and uuid to events
    for ev in noisy["events"]:
        ev["ts"] = 1234567890
        ev["captured_at"] = "2026-06-29T12:00:01Z"
        ev["uuid"] = f"uuid-{ev['id']}"
    # Add uuid to claims (if dict)
    claims = noisy["claims"]
    if isinstance(claims, dict):
        for cid in claims:
            claims[cid]["uuid"] = f"uuid-{cid}"
    else:
        # claims as list of dicts
        for cl in claims:
            if isinstance(cl, dict):
                cl["uuid"] = f"uuid-{cl.get('id', 'unknown')}"
    # Add uuid to falsifiers
    for f in noisy.get("falsifiers", []):
        f["uuid"] = f"uuid-{f['id']}"
    # Reorder events list
    noisy["events"] = sorted(noisy["events"], key=lambda x: x["id"], reverse=True)
    # Reorder claims dict by reversing key order (if dict) or reverse list
    if isinstance(noisy["claims"], dict):
        sorted_items = sorted(noisy["claims"].items(), key=lambda x: x[0], reverse=True)
        noisy["claims"] = dict(sorted_items)
    else:
        noisy["claims"] = sorted(noisy["claims"], key=lambda x: x["id"] if isinstance(x, dict) else x, reverse=True)
    # Reorder falsifiers
    noisy["falsifiers"] = sorted(noisy["falsifiers"], key=lambda x: x["id"], reverse=True)
    return noisy


# ---------------------------------------------------------------------------
# Fixture-based tests (load the synthetic trace)
# ---------------------------------------------------------------------------

def test_stability_and_prefix():
    trace = _load_trace()
    fp1 = behavioral_fingerprint(trace)
    fp2 = behavioral_fingerprint(trace)
    assert fp1 == fp2
    assert fp1.startswith("fp:sha256:")


def test_volatile_noise_invariance():
    trace = _load_trace()
    noisy = _add_noise(trace)

    # Naive hash must differ
    naive_orig = _naive_hash(trace)
    naive_noisy = _naive_hash(noisy)
    assert naive_orig != naive_noisy, "Naive hash should differ after noise"

    # Behavioral fingerprint must be identical
    fp_orig = behavioral_fingerprint(trace)
    fp_noisy = behavioral_fingerprint(noisy)
    assert fp_orig == fp_noisy


def test_decision_change_polarity():
    trace = _load_trace()
    modified = deepcopy(trace)
    # Flip the first event's first target polarity to a DIFFERENT real value.
    for ev in modified["events"]:
        targets = ev.get("targets", [])
        if targets:
            cur = targets[0]["polarity"]
            targets[0]["polarity"] = "refute" if cur == "corroborate" else "corroborate"
            break
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_decision_change_claim_statement():
    trace = _load_trace()
    modified = deepcopy(trace)
    claims = modified["claims"]
    if isinstance(claims, dict):
        for cid in list(claims.keys()):
            claims[cid]["statement"] = "OVERRIDDEN STATEMENT"
            break
    else:
        for cl in claims:
            if isinstance(cl, dict) and "statement" in cl:
                cl["statement"] = "OVERRIDDEN STATEMENT"
                break
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_decision_change_claim_status():
    trace = _load_trace()
    modified = deepcopy(trace)
    claims = modified["claims"]
    if isinstance(claims, dict):
        for cid in list(claims.keys()):
            claims[cid]["status"] = "OVERRIDDEN_STATUS"
            break
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_decision_change_add_claim():
    trace = _load_trace()
    modified = deepcopy(trace)
    new_claim = {"id": "Z_NEW_CLAIM", "statement": "extra claim", "status": "active"}
    claims = modified["claims"]
    if isinstance(claims, dict):
        claims["Z_NEW_CLAIM"] = new_claim
    else:
        modified["claims"].append(new_claim)
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_decision_change_remove_event():
    trace = _load_trace()
    modified = deepcopy(trace)
    if modified["events"]:
        modified["events"].pop(0)
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_decision_change_remove_claim():
    trace = _load_trace()
    modified = deepcopy(trace)
    claims = modified["claims"]
    if isinstance(claims, dict) and claims:
        key = next(iter(claims))
        del claims[key]
    elif isinstance(claims, list) and claims:
        # remove the first dict (assume dicts)
        modified["claims"] = [c for c in claims if not isinstance(c, dict) or c.get("id") != claims[0].get("id")]
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_nested_targets_reorder_invariance():
    """Reordering an event's nested `targets` list (not a decision change) must NOT change the
    fingerprint — nested decision-collections are canonicalized, like the top-level collections."""
    trace = {
        "events": [{
            "id": "E1",
            "targets": [
                {"claim": "C1", "polarity": "corroborate"},
                {"claim": "C2", "polarity": "refute"},
                {"claim": "C3", "polarity": "corroborate"},
            ],
        }],
        "claims": {"C1": {"id": "C1"}},
        "falsifiers": [],
    }
    reordered = deepcopy(trace)
    reordered["events"][0]["targets"] = list(reversed(reordered["events"][0]["targets"]))
    assert behavioral_fingerprint(reordered) == behavioral_fingerprint(trace)
    # but actually changing a target's polarity DOES change it
    changed = deepcopy(trace)
    changed["events"][0]["targets"][0]["polarity"] = "refute"
    assert behavioral_fingerprint(changed) != behavioral_fingerprint(trace)


def test_decision_change_event_statement():
    trace = _load_trace()
    modified = deepcopy(trace)
    modified["events"][0]["statement"] = "OVERRIDDEN EVENT STATEMENT"
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)


def test_decision_change_falsifier():
    trace = _load_trace()
    # change a falsifier statement
    modified = deepcopy(trace)
    modified["falsifiers"][0]["statement"] = "OVERRIDDEN FALSIFIER"
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(modified)
    # add a falsifier
    added = deepcopy(trace)
    added["falsifiers"].append({"id": "F999", "statement": "new falsifier"})
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(added)
    # remove a falsifier
    removed = deepcopy(trace)
    removed["falsifiers"].pop(0)
    assert behavioral_fingerprint(trace) != behavioral_fingerprint(removed)


def test_claim_parent_or_hash_change_is_NOT_masked():
    """parent/hash are decision-bearing claim body fields, NOT volatile — a change must register."""
    trace = _load_trace()
    first_cid = next(iter(trace["claims"]))
    base = behavioral_fingerprint(trace)
    parented = deepcopy(trace)
    parented["claims"][first_cid]["parent"] = "C_PARENT_X"
    assert behavioral_fingerprint(parented) != base
    hashed = deepcopy(trace)
    hashed["claims"][first_cid]["hash"] = "abc123digest"
    assert behavioral_fingerprint(hashed) != base


def test_ledger_slice_shape_invariant_to_commit_shas():
    """A ledger_slice trace (events+claims, no falsifiers) projects cleanly: its top-level commit
    SHAs / branch / range are dropped (volatile), but an event decision change still registers."""
    slice_trace = {
        "commit_range": "HEAD~3..HEAD",
        "commits": ["aaaaaaa", "bbbbbbb", "ccccccc"],
        "branch": "r3-harness/kimi",
        "event_count": 1,
        "events": [{"id": "E1", "targets": [{"claim": "C1", "polarity": "corroborate"}]}],
        "claims": ["C1"],
    }
    base = behavioral_fingerprint(slice_trace)
    # different run: new commit shas / branch / range -> SAME fingerprint
    other = deepcopy(slice_trace)
    other["commits"] = ["zzzzzzz", "yyyyyyy"]
    other["commit_range"] = "main~9..main"
    other["branch"] = "r3-harness/gemini"
    other["event_count"] = 99
    assert behavioral_fingerprint(other) == base
    # a real decision change (polarity) -> DIFFERENT fingerprint
    changed = deepcopy(slice_trace)
    changed["events"][0]["targets"][0]["polarity"] = "refute"
    assert behavioral_fingerprint(changed) != base


# ---------------------------------------------------------------------------
# strip_volatile unit tests
# ---------------------------------------------------------------------------

def test_strip_volatile_keeps_id():
    inp = {"id": "keep_me", "uuid": "drop", "nested": {"id": "also_keep", "ts": 0}}
    result = strip_volatile(inp)
    assert "id" in result
    assert "uuid" not in result
    assert "id" in result["nested"]
    assert "ts" not in result["nested"]


def test_strip_volatile_inside_list():
    inp = [{"id": "a", "timestamp": 1}, {"id": "b", "nonce": 42}]
    result = strip_volatile(inp)
    assert len(result) == 2
    for item in result:
        assert "id" in item
        assert "timestamp" not in item
        assert "nonce" not in item
    # original unchanged
    assert "timestamp" in inp[0]


def test_strip_volatile_no_mutation():
    inp = {"id": "x", "uuid": "y", "ts": 1}
    orig = deepcopy(inp)
    _ = strip_volatile(inp)
    assert inp == orig


def test_strip_volatile_deep_nested():
    inp = {"outer": {"id": "o", "uuid": "drop", "inner": {"id": "i", "session_id": "drop"}}}
    result = strip_volatile(inp)
    assert "uuid" not in result["outer"]
    assert "session_id" not in result["outer"]["inner"]
    assert result["outer"]["id"] == "o"
    assert result["outer"]["inner"]["id"] == "i"


# ---------------------------------------------------------------------------
# extract_decisions unit tests
# ---------------------------------------------------------------------------

def test_extract_decisions_claims_dict():
    trace = {
        "ref": "abc",
        "events": [{"id": "ev1", "foo": "bar", "uuid": "drop"}],
        "claims": {
            "c2": {"id": "c2", "statement": "X", "status": "active", "ts": 123},
            "c1": {"id": "c1", "statement": "Y", "status": "active", "ts": 456},
        },
        "falsifiers": [{"id": "f1", "statement": "fake", "uuid": "drop"}],
    }
    dec = extract_decisions(trace)
    # events sorted by id
    assert [e["id"] for e in dec["events"]] == ["ev1"]
    assert "uuid" not in dec["events"][0]
    # claims sorted by id
    assert [c["id"] for c in dec["claims"]] == ["c1", "c2"]
    for c in dec["claims"]:
        assert "id" in c
        assert "ts" not in c  # volatile key dropped
    # falsifiers sorted by id
    assert [f["id"] for f in dec["falsifiers"]] == ["f1"]
    assert "uuid" not in dec["falsifiers"][0]


def test_extract_decisions_dict_key_is_authoritative_id():
    """When a claims dict key diverges from the body's own 'id', the KEY wins (the lookup
    identity), so a corrupt/stale body id cannot shadow it."""
    trace = {
        "events": [{"id": "ev1"}],
        "claims": {"KEY_ID": {"id": "BODY_ID_DIVERGENT", "statement": "X"}},
        "falsifiers": [],
    }
    dec = extract_decisions(trace)
    assert [c["id"] for c in dec["claims"]] == ["KEY_ID"]
    assert dec["claims"][0]["statement"] == "X"


def test_extract_decisions_claims_list_ids():
    trace = {
        "ref": "abc",
        "events": [{"id": "ev1"}],
        "claims": ["c2", "c1"],  # list of ids (ledger_slice style)
        "falsifiers": [],
    }
    dec = extract_decisions(trace)
    assert [c["id"] for c in dec["claims"]] == ["c1", "c2"]


def test_extract_decisions_claims_list_dicts():
    trace = {
        "ref": "abc",
        "events": [{"id": "ev1"}],
        "claims": [
            {"id": "c2", "statement": "X"},
            {"id": "c1", "statement": "Y"},
        ],
        "falsifiers": [],
    }
    dec = extract_decisions(trace)
    assert [c["id"] for c in dec["claims"]] == ["c1", "c2"]
    assert dec["claims"][0]["statement"] == "Y"


def test_extract_decisions_fallback_no_trace_keys():
    """When trace lacks events/claims/falsifiers, fall back to strip_volatile."""
    inp = {"id": "some_obj", "uuid": "drop", "list": [1, 2]}
    dec = extract_decisions(inp)
    assert dec == {"id": "some_obj", "list": [1, 2]}
