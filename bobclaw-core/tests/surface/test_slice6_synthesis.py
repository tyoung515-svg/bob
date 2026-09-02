"""MS#4 · P1 — Slice 6 tests (ForestOS synthesis grant + reactive approval, D-3).

Synthesis is not ambient: the SynthesisRetrieval handle is minted only from an approved, scoped,
decaying grant, and can never query outside its scope or after expiry.
"""
import pytest

from core.surface import (
    EscalationRequest,
    EscalationTier,
    NotebookBoundProvider,
    SynthesisError,
    SynthesisRetrieval,
    create_synthesis_grant,
    request_synthesis,
)


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    async def search(self, instance_name, *, query_vector=None, k=10, filters=None):
        self.calls.append(filters)
        return [instance_name]


def _grant(now=0.0, decay=3600.0):
    esc = EscalationRequest(EscalationTier.QUORUM, ["nb-A", "nb-B"], "ambiguous_span")
    req = request_synthesis(esc)
    return create_synthesis_grant(req, "travis", now=now, decay_seconds=decay, approved=True)


# ── D-3 reactive request ─────────────────────────────────────────────────────

def test_grounded_escalation_makes_no_request():
    esc = EscalationRequest(EscalationTier.GROUNDED, ["nb-A"], "grounded")
    assert request_synthesis(esc) is None            # no synthesis when grounded


def test_escalation_makes_legible_request():
    esc = EscalationRequest(EscalationTier.FULL_FANOUT, ["nb-A", "nb-B"], "far_from_all")
    req = request_synthesis(esc)
    assert req.notebook_ids == ("nb-A", "nb-B") and req.tier == "full_fanout"


# ── grant minting (no ambient synthesis) ─────────────────────────────────────

def test_grant_requires_approval():
    esc = EscalationRequest(EscalationTier.QUORUM, ["nb-A", "nb-B"], "x")
    req = request_synthesis(esc)
    with pytest.raises(SynthesisError):
        create_synthesis_grant(req, "travis", now=0.0, approved=False)   # unapproved → refused


def test_grant_scope_and_decay():
    g = _grant(now=100.0, decay=3600.0)
    assert g.covers("nb-A") and g.covers("nb-B") and not g.covers("nb-C")
    assert g.is_valid(now=200.0) and not g.is_valid(now=100.0 + 3600.0 + 1)


# ── SynthesisRetrieval: scoped, decaying, no roam ────────────────────────────

async def test_synthesis_queries_only_in_scope():
    g = _grant()
    provs = {
        "nb-A": NotebookBoundProvider(_FakeAdapter(), notebook_id="nb-A", instance_name="wiki"),
        "nb-B": NotebookBoundProvider(_FakeAdapter(), notebook_id="nb-B", instance_name="wiki"),
    }
    h = SynthesisRetrieval(g, provs, clock=lambda: 1.0)
    assert await h.vector_search("nb-A", [0.1]) == ["wiki"]      # in scope → delegates
    with pytest.raises(SynthesisError):
        await h.vector_search("nb-C", [0.1])                     # outside scope → refused (no roam)


def test_provider_outside_scope_rejected_at_construction():
    g = _grant()
    provs = {"nb-Z": NotebookBoundProvider(_FakeAdapter(), notebook_id="nb-Z", instance_name="wiki")}
    with pytest.raises(SynthesisError):
        SynthesisRetrieval(g, provs, clock=lambda: 1.0)          # nb-Z not in grant scope


def test_expired_grant_rejected():
    g = _grant(now=0.0, decay=10.0)
    provs = {"nb-A": NotebookBoundProvider(_FakeAdapter(), notebook_id="nb-A", instance_name="wiki")}
    with pytest.raises(SynthesisError):
        SynthesisRetrieval(g, provs, clock=lambda: 999.0)        # already decayed
