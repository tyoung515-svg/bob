"""Test-pipe — results ledger + report (SPEC §9 unit 10, §10). Synthetic per-config."""
from __future__ import annotations

from core.testpipe.ledger import Ledger, UpliftTriple, uplift_per_dollar
from core.testpipe.types import ConfigResult


def _cr(name, n, hidden, cost, provenance=None):
    return ConfigResult(name=name, n_items=n, n_hidden_pass=hidden,
                        n_visible_pass=hidden, cost_usd=cost,
                        provenance=provenance or {})


def _ledger():
    lg = Ledger()
    lg.add(_cr("B0", 10, 3, 0.10, {"worker": "local", "audit": "local", "front": "local"}))
    lg.add(_cr("audit_S", 10, 7, 0.50, {"worker": "local", "audit": "hosted", "front": "local"}))
    lg.add(_cr("bob_full", 10, 8, 0.80, {"worker": "hosted", "audit": "hosted", "front": "hosted"}))
    return lg


def test_uplift_per_dollar():
    assert uplift_per_dollar(0.4, 0.4) == 1.0
    assert uplift_per_dollar(0.4, 0.0) == 0.0        # no positive cost ⇒ 0


def test_slot_delta_attributes_one_axis():
    d = _ledger().slot_delta("audit", "B0", "audit_S")
    assert round(d.delta_pass, 2) == 0.40            # 0.7 - 0.3
    assert round(d.delta_cost_usd, 2) == 0.40        # 0.50 - 0.10
    assert d.uplift_per_dollar == 1.0
    # provenance carried so the gain is labelled hosted-audit, not "all-local" (§7.4)
    assert d.variant_provenance["audit"] == "hosted"


def test_uplift_triple_and_headroom():
    triple = _ledger().uplift_triple("B0", "bob_full", bare_at_n_oracle=0.9)
    assert isinstance(triple, UpliftTriple)
    assert triple.bare_at_1 == 0.3 and triple.bob_final == 0.8
    assert triple.bob_over_bare == 0.5
    # captured 0.5 of the 0.6 (0.3→0.9) headroom
    assert round(triple.headroom_captured, 4) == round(0.5 / 0.6, 4)


def test_report_labels_all_local_honestly():
    lg = _ledger()
    triple = lg.uplift_triple("B0", "bob_full", bare_at_n_oracle=0.9)
    report = lg.report(deltas=[("audit", "B0", "audit_S")], triple=triple)
    by = {c["name"]: c for c in report["configs"]}
    assert by["B0"]["all_local"] is True                 # every slot local
    assert by["audit_S"]["all_local"] is False           # hosted audit ⇒ NOT all-local
    assert report["slot_deltas"][0]["axis"] == "audit"
    assert report["uplift_triple"]["bob_final"] == 0.8
    assert report["uplift_triple_derived"]["bob_over_bare"] == 0.5
