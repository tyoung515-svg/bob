"""Unit tests for the P4 stored-side vector integrity probe. No network — duck-typed fake client."""
from unittest.mock import MagicMock

import pytest

from core.memory.integrity import (
    ZERO_NORM_EPSILON,
    IntegrityProbeError,
    VectorIntegrityReport,
    assert_collection_healthy,
    is_zero_vector,
    probe_collection,
)

DIM = 768


class FakePoint:
    def __init__(self, vector):
        self.vector = vector


class FakeClient:
    """Duck-typed Qdrant stand-in with real offset paging semantics."""

    def __init__(self, points=None, raises=None, page_size=None):
        self.points = list(points) if points is not None else []
        self.raises = raises
        self.page_size = page_size  # None => honour the caller's limit exactly
        self.calls = []

    def scroll(self, collection, limit=None, with_vectors=None, offset=None):
        self.calls.append((collection, limit, with_vectors, offset))
        if self.raises is not None:
            raise self.raises
        start = offset or 0
        n = min(limit, self.page_size) if self.page_size else limit
        chunk = self.points[start:start + n]
        nxt = start + len(chunk)
        return chunk, (nxt if nxt < len(self.points) else None)


def live_vec(seed=0.5):
    return [seed] + [0.0] * (DIM - 1)


def zero_vec():
    return [0.0] * DIM


# ---------------------------------------------------------------- is_zero_vector

def test_is_zero_vector_detects_null_and_live():
    assert is_zero_vector(zero_vec()) is True
    assert is_zero_vector(live_vec()) is False


def test_is_zero_vector_empty_is_zero():
    assert is_zero_vector([]) is True
    assert is_zero_vector(None) is True


def test_is_zero_vector_epsilon_boundary():
    assert is_zero_vector([ZERO_NORM_EPSILON / 2] + [0.0] * (DIM - 1)) is True
    assert is_zero_vector([ZERO_NORM_EPSILON * 100] + [0.0] * (DIM - 1)) is False


def test_is_zero_vector_unsupported_shape_raises_probe_error():
    """audit F7 — a sparse/multivector shape must surface as IntegrityProbeError, not a raw TypeError."""
    with pytest.raises(IntegrityProbeError, match="unsupported vector shape"):
        is_zero_vector([[0.1, 0.2], [0.3, 0.4]])


# ---------------------------------------------------------------- probe_collection

def test_probe_all_zero_is_corrupt():
    """The 2026-06-20 shape: every point stored, every vector null."""
    r = probe_collection(FakeClient([FakePoint(zero_vec()) for _ in range(5)]), "wiki_chunks")
    assert (r.sampled, r.zero, r.live) == (5, 5, 0)
    assert r.healthy is False
    assert r.zero_fraction == 1.0
    assert "CORRUPT" in r.summary()


def test_probe_all_live_is_healthy():
    r = probe_collection(FakeClient([FakePoint(live_vec(0.1 * i + 0.1)) for i in range(4)]), "wiki_chunks")
    assert (r.sampled, r.zero, r.live) == (4, 0, 4)
    assert r.healthy is True and r.zero_fraction == 0.0
    assert "OK" in r.summary()


def test_probe_partial_corruption_is_not_healthy():
    """One null vector in an otherwise-live sample must still fail — a canary, not a vote."""
    c = FakeClient([FakePoint(live_vec()), FakePoint(zero_vec()), FakePoint(live_vec())])
    r = probe_collection(c, "wiki_chunks")
    assert (r.sampled, r.zero, r.live) == (3, 1, 2)
    assert r.healthy is False


def test_probe_requests_vectors_and_never_writes():
    """audit F5 — assert against a MagicMock (which HAS every write method) so this can actually fail."""
    client = MagicMock()
    client.scroll.return_value = ([FakePoint(live_vec())], None)
    probe_collection(client, "wiki_chunks", sample=50)
    # It must ask for vectors, or it cannot see the corruption at all.
    assert client.scroll.call_args.kwargs["with_vectors"] is True
    # It must not write. MagicMock exposes upsert/delete/... so a write WOULD register here.
    client.upsert.assert_not_called()
    client.delete.assert_not_called()
    client.set_payload.assert_not_called()
    client.create_collection.assert_not_called()
    client.delete_collection.assert_not_called()


def test_probe_empty_sample_is_not_healthy():
    """FAIL-CLOSED: nothing sampled == unverified, not proven-good."""
    r = probe_collection(FakeClient([]), "empty_coll")
    assert (r.sampled, r.zero, r.live) == (0, 0, 0)
    assert r.healthy is False
    assert "unverified" in r.summary()


def test_probe_named_vectors_all_checked():
    """audit F7 — every named vector is checked; dict order must not decide the verdict."""
    c = FakeClient([
        FakePoint({"dense": live_vec(), "sparse": zero_vec()}),  # half-corrupt => condemned
        FakePoint({"dense": live_vec(), "sparse": live_vec(0.3)}),
    ])
    r = probe_collection(c, "named")
    assert (r.sampled, r.zero, r.live) == (2, 1, 1)
    assert r.healthy is False


def test_probe_point_without_vector_is_not_counted():
    r = probe_collection(FakeClient([FakePoint(None), FakePoint(live_vec()), FakePoint({})]), "c")
    assert (r.sampled, r.zero, r.live) == (1, 0, 1)


def test_probe_scroll_failure_raises_not_healthy():
    """An unreadable collection is an UNKNOWN and must raise — never silently report healthy."""
    with pytest.raises(IntegrityProbeError, match="could not scroll"):
        probe_collection(FakeClient(raises=RuntimeError("connection refused")), "wiki_chunks")


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_probe_rejects_bad_collection(bad):
    with pytest.raises(IntegrityProbeError, match="invalid collection"):
        probe_collection(FakeClient(), bad)


@pytest.mark.parametrize("bad", [0, -1, True, "10"])
def test_probe_rejects_bad_sample(bad):
    with pytest.raises(IntegrityProbeError, match="sample must be"):
        probe_collection(FakeClient([FakePoint(live_vec())]), "c", sample=bad)


# ---------------------------------------------------------------- paging / exhaustiveness (audit F4)

def test_probe_pages_to_exhaustion_when_sample_is_none():
    """sample=None must walk the WHOLE collection, not just the first page."""
    pts = [FakePoint(live_vec()) for _ in range(500)] + [FakePoint(zero_vec())]  # corruption at the END
    c = FakeClient(pts, page_size=64)
    r = probe_collection(c, "wiki_chunks", sample=None, page=64)
    assert r.sampled == 501 and r.zero == 1
    assert r.exhaustive is True
    assert r.healthy is False, "a late-batch corruption must be caught by an exhaustive scan"
    assert len(c.calls) > 1, "must have paged"


def test_probe_capped_sample_is_marked_non_exhaustive():
    """A cap only ever sees the first N ids — the report must SAY it's a spot-check."""
    pts = [FakePoint(live_vec()) for _ in range(300)] + [FakePoint(zero_vec())]
    r = probe_collection(FakeClient(pts, page_size=64), "wiki_chunks", sample=100, page=64)
    assert r.sampled == 100 and r.zero == 0
    assert r.exhaustive is False
    assert "SPOT-CHECK" in r.summary()


def test_probe_full_scan_marked_exhaustive_when_cap_exceeds_size():
    r = probe_collection(FakeClient([FakePoint(live_vec()) for _ in range(10)]), "c", sample=999)
    assert r.sampled == 10 and r.exhaustive is True
    assert "full scan" in r.summary()


# ---------------------------------------------------------------- assert_collection_healthy

def test_assert_healthy_passes_on_live():
    r = assert_collection_healthy(FakeClient([FakePoint(live_vec())]), "c")
    assert isinstance(r, VectorIntegrityReport) and r.healthy is True and r.exhaustive is True


def test_assert_healthy_raises_on_corrupt():
    """The post-reindex gate LKS CLAUDE.md demands: 'Always verify zero-count == 0 after any reindex.'"""
    with pytest.raises(IntegrityProbeError, match="CORRUPT"):
        assert_collection_healthy(FakeClient([FakePoint(zero_vec()) for _ in range(3)]), "wiki_chunks")


def test_assert_healthy_raises_on_empty():
    with pytest.raises(IntegrityProbeError, match="unverified"):
        assert_collection_healthy(FakeClient([]), "empty")


def test_assert_healthy_scans_exhaustively_by_default():
    """audit F4 — the gate must not pass a collection whose tail it never looked at."""
    pts = [FakePoint(live_vec()) for _ in range(400)] + [FakePoint(zero_vec())]
    with pytest.raises(IntegrityProbeError, match="CORRUPT"):
        assert_collection_healthy(FakeClient(pts, page_size=64), "wiki_chunks")
