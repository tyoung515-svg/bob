import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.memory.lks_adapter import (
    LKSReadAdapter,
    ReadAdapterError,
    InstanceACL,
    read_instance_acl,
    enforce_read_acl,
)
from core.memory.exceptions import ACLViolation
from core.memory.fingerprint import (
    EmbedFingerprint,
    FingerprintMissing,
    FingerprintMismatch,
    stamp_meta,
    SENTINEL_POINT_ID,
    SENTINEL_MARKER_KEY,
)
from core.memory.models import Hit, SlotResolution
from core.ledger.federation import FederationRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 768


def vec(seed=0.1):
    """A 768-d non-degenerate vector using a single seed value."""
    return [seed] + [0.0] * (DIM - 1)


def slot(model="model-a", dim=DIM):
    return SlotResolution(
        slot_name="embed_text",
        model=model,
        backend="lmstudio",
        endpoint="http://x",
        embedding_dimension=dim,
    )


def fp(model="model-a", dim=DIM):
    return EmbedFingerprint(model, dim, True, "cosine")


def acl_block(readers=("bobclaw", "lks"), mode="ro", writer="lks"):
    return {"writer": writer, "readers": list(readers), "mode": mode}


def make_registry(tmp_path, *, meta):
    """Register one instance 'inst' -> collection 'c', dim DIM."""
    reg = FederationRegistry(tmp_path / "reg.json")
    reg.register("inst", "/repos/_t", collection="c", dim=DIM, meta=meta)
    return reg


class FakeEmbedder:
    """Async embedder returning a fixed vector for any input."""

    def __init__(self, out):
        self._out = out
        self.calls = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [list(self._out)]


def point(pid, score, payload=None):
    return SimpleNamespace(id=pid, score=score, payload=payload or {})


def resp(points):
    return SimpleNamespace(points=list(points))


def full_meta(acl=None, model="model-a"):
    """Collated meta with acl + fingerprint."""
    a = acl or acl_block()
    return stamp_meta({"acl": a}, fp(model=model))


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestLKSReadAdapter:

    async def test_resolve_search_happy_vector_path(self, tmp_path):
        meta = stamp_meta({"acl": acl_block()}, fp())
        reg = make_registry(tmp_path, meta=meta)
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = resp([
            point("a", 0.9, {"text": "A"}),
            point("b", 0.8, {}),
            point("c", 0.7, {}),
        ])
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), reader_id="bobclaw")
        hits = await ad.search("inst", query_vector=vec(), k=3)

        assert [h.id for h in hits] == ["a", "b", "c"]
        assert [round(h.score, 3) for h in hits] == [0.9, 0.8, 0.7]
        assert isinstance(hits[0], Hit)
        assert client.query_points.call_args.kwargs["collection_name"] == "c"
        # READ-ONLY: no mutating client call happened
        assert client.upsert.call_count == 0
        assert client.delete.call_count == 0
        assert client.create_collection.call_count == 0
        assert client.delete_collection.call_count == 0

    async def test_text_query_uses_embedder(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        emb = FakeEmbedder(vec(0.2))
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = resp([point("a", 0.5, {})])
        ad = LKSReadAdapter(reg, client=client, embedder=emb, live_slot=slot())
        hits = await ad.search("inst", query="hello", k=1)

        assert emb.calls == [["hello"]]
        assert client.query_points.call_args.kwargs["query"] == vec(0.2)
        assert [h.id for h in hits] == ["a"]

    async def test_exactly_one_of_query_args(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        emb = FakeEmbedder(vec(0.1))
        ad = LKSReadAdapter(reg, client=client, embedder=emb, live_slot=slot())

        with pytest.raises(ReadAdapterError):
            await ad.search("inst", k=3)  # neither
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query="x", query_vector=vec(), k=3)  # both

    async def test_dim_mismatch_fail_closed(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=[0.1, 0.2, 0.3], k=3)  # dim=3
        assert client.query_points.call_count == 0

        emb = FakeEmbedder([0.1, 0.2])  # dim=2
        ad2 = LKSReadAdapter(reg, client=client, embedder=emb, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad2.search("inst", query="x")

    async def test_k_must_be_positive(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=0)
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=-1)

    async def test_fingerprint_mismatch_fail_closed(self, tmp_path):
        meta = stamp_meta({"acl": acl_block()}, fp(model="model-a"))
        reg = make_registry(tmp_path, meta=meta)
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(model="model-b"))  # same dim, diff model
        client.query_points.return_value = resp([point("a", 0.9, {})])
        with pytest.raises(FingerprintMismatch):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0  # gate precedes search

    async def test_fingerprint_missing_fail_closed_and_soft(self, tmp_path):
        meta_no_fp = {"acl": acl_block()}
        reg = make_registry(tmp_path, meta=meta_no_fp)
        client = MagicMock()

        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), require_stamp=True)
        with pytest.raises(FingerprintMissing):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0  # gate precedes search

        client.collection_exists.return_value = True
        client.query_points.return_value = resp([point("a", 0.9, {})])
        ad_soft = LKSReadAdapter(reg, client=client, live_slot=slot(), require_stamp=False)
        hits = await ad_soft.search("inst", query_vector=vec(), k=3)
        assert [h.id for h in hits] == ["a"]

    async def test_stamp_present_but_no_live_slot_refused(self, tmp_path):
        meta = stamp_meta({"acl": acl_block()}, fp())
        reg = make_registry(tmp_path, meta=meta)
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=None)  # cannot verify
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0

    async def test_acl_enforcement(self, tmp_path):
        # (a) non-reader denied
        meta_a = stamp_meta({"acl": acl_block(readers=["lks"])}, fp())
        reg = make_registry(tmp_path, meta=meta_a)
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), reader_id="bobclaw")
        with pytest.raises(ACLViolation):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0  # ACL gate precedes search

        # (b) reader in list passes
        reg2 = make_registry(tmp_path, meta=meta_a)
        client2 = MagicMock()
        client2.collection_exists.return_value = True
        client2.query_points.return_value = resp([point("a", 0.9, {})])
        ad2 = LKSReadAdapter(reg2, client=client2, live_slot=slot(), reader_id="lks")
        assert [h.id for h in await ad2.search("inst", query_vector=vec(), k=3)] == ["a"]

        # (c) wildcard passes any reader
        meta_wild = stamp_meta({"acl": acl_block(readers=["*"])}, fp())
        reg3 = make_registry(tmp_path, meta=meta_wild)
        client3 = MagicMock()
        client3.collection_exists.return_value = True
        client3.query_points.return_value = resp([point("b", 0.8, {})])
        ad3 = LKSReadAdapter(reg3, client=client3, live_slot=slot(), reader_id="anybody")
        assert [h.id for h in await ad3.search("inst", query_vector=vec(), k=3)] == ["b"]

        # (d) mode="wo" -> denied
        meta_wo = stamp_meta({"acl": acl_block(mode="wo")}, fp())
        reg4 = make_registry(tmp_path, meta=meta_wo)
        client4 = MagicMock()
        ad4 = LKSReadAdapter(reg4, client=client4, live_slot=slot(), reader_id="bobclaw")
        with pytest.raises(ACLViolation):
            await ad4.search("inst", query_vector=vec(), k=3)

        # (e) malformed acl
        with pytest.raises(ACLViolation):
            read_instance_acl({"acl": {"mode": "ro"}})  # missing readers
        with pytest.raises(ACLViolation):
            read_instance_acl({"acl": "nope"})  # not a dict

        # (f) undeclared acl + require_acl=True -> ACLViolation
        meta_no_acl = stamp_meta({}, fp())
        reg5 = make_registry(tmp_path, meta=meta_no_acl)
        client5 = MagicMock()
        ad5 = LKSReadAdapter(reg5, client=client5, live_slot=slot(), require_acl=True)
        with pytest.raises(ACLViolation):
            await ad5.search("inst", query_vector=vec(), k=3)

        # undeclared acl + default require_acl=False -> passes
        reg6 = make_registry(tmp_path, meta=meta_no_acl)
        client6 = MagicMock()
        client6.collection_exists.return_value = True
        client6.query_points.return_value = resp([point("c", 0.7, {})])
        ad6 = LKSReadAdapter(reg6, client=client6, live_slot=slot(), require_acl=False)
        assert [h.id for h in await ad6.search("inst", query_vector=vec(), k=3)] == ["c"]

        # direct unit checks
        assert read_instance_acl(None) is None
        assert read_instance_acl({"note": "x"}) is None
        good = InstanceACL(writer="lks", readers=frozenset({"bobclaw"}), mode="ro")
        assert enforce_read_acl(good, "bobclaw") is None

    def test_read_only_structural(self, tmp_path):
        reg = make_registry(tmp_path, meta={})
        ad = LKSReadAdapter(reg, client=MagicMock(), live_slot=slot())
        for attr in ("index", "upsert", "delete", "write", "create_collection", "delete_collection"):
            assert not hasattr(ad, attr), f"unexpected write attribute {attr!r} on LKSReadAdapter"

    async def test_sentinel_filtered_from_results(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = resp([
            point("a", 0.95, {"text": "A"}),
            point(SENTINEL_POINT_ID, 0.99, {SENTINEL_MARKER_KEY: True}),
            point("b", 0.90, {}),
        ])
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        hits = await ad.search("inst", query_vector=vec(), k=2)

        ids = [h.id for h in hits]
        assert SENTINEL_POINT_ID not in ids
        assert all(not h.payload.get(SENTINEL_MARKER_KEY) for h in hits)
        assert ids == ["a", "b"]
        assert client.query_points.call_args.kwargs["limit"] == 3  # over-fetch k+1

    async def test_sentinel_filtered_by_id_without_marker(self, tmp_path):
        """The id check must drop the sentinel even if its payload marker is absent."""
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = resp([
            point("a", 0.95, {"text": "A"}),
            point(SENTINEL_POINT_ID, 0.99, {"text": "sentinel payload without marker"}),
            point("b", 0.90, {}),
        ])
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        hits = await ad.search("inst", query_vector=vec(), k=2)

        ids = [h.id for h in hits]
        assert SENTINEL_POINT_ID not in ids
        assert ids == ["a", "b"]

    async def test_filters_passed_through(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = resp([])
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        await ad.search("inst", query_vector=vec(), k=3, filters={"source_path": "a.md"})
        assert client.query_points.call_args.kwargs["query_filter"] is not None

    async def test_missing_collection_returns_empty(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.return_value = False
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        hits = await ad.search("inst", query_vector=vec(), k=3)
        assert hits == []
        assert client.query_points.call_count == 0

    async def test_parity_with_independent_search(self, tmp_path):
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.return_value = True
        pts = [
            point("a", 0.9, {}),
            point(SENTINEL_POINT_ID, 0.99, {SENTINEL_MARKER_KEY: True}),
            point("b", 0.8, {}),
            point("c", 0.7, {}),
        ]
        client.query_points.return_value = resp(pts)
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        hits = await ad.search("inst", query_vector=vec(), k=3)

        raw = [(str(p.id), float(p.score)) for p in pts if str(p.id) != SENTINEL_POINT_ID]
        raw.sort(key=lambda t: t[1], reverse=True)
        raw = raw[:3]
        assert [(h.id, round(h.score, 6)) for h in hits] == [(i, round(s, 6)) for i, s in raw]

    # --- audit r1 regressions ---

    async def test_malformed_fingerprint_fails_closed_even_soft(self, tmp_path):
        """A PRESENT-but-malformed embed stamp fails closed (FingerprintError) even with require_stamp=False."""
        from core.memory.fingerprint import FingerprintError
        meta = {"acl": acl_block(), "embed": {"model_id": "m"}}  # malformed (missing dim/normalize/distance)
        reg = make_registry(tmp_path, meta=meta)
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), require_stamp=False)
        with pytest.raises(FingerprintError):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0  # corruption never silently skipped to a search

    async def test_collection_exists_error_surfaces_failclosed(self, tmp_path):
        """A connectivity failure surfaces as a descriptive ReadAdapterError, never a silent empty result."""
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.side_effect = RuntimeError("conn refused")
        client.query_points.side_effect = RuntimeError("conn refused")
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=3)

    async def test_garbled_instance_dim_fails_closed(self, tmp_path):
        """Defense-in-depth: a garbled ResolvedInstance.dim (None) fails closed (ReadAdapterError, not TypeError)."""
        from core.ledger.federation import ResolvedInstance
        reg = make_registry(tmp_path, meta=full_meta())
        bad = ResolvedInstance(
            name="inst", repo="r", ledger_dir="ledger", collection="c", dim=None, meta=full_meta()
        )
        reg.resolve = lambda name: bad  # force a garbled dim past federation's own guard
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0

    # --- audit r2 regressions ---

    async def test_text_query_without_embedder_fails_closed(self, tmp_path):
        """A text query with no embedder configured raises ReadAdapterError (no silent fallback)."""
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), embedder=None)
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query="hello", k=3)
        assert client.query_points.call_count == 0

    async def test_empty_reader_entries_and_blank_reader_fail_closed(self, tmp_path):
        """An empty-string reader entry is malformed; a blank reader_id is never granted (even vs '*')."""
        with pytest.raises(ACLViolation):
            read_instance_acl({"acl": acl_block(readers=["", "lks"])})
        wild = InstanceACL(writer=None, readers=frozenset({"*"}), mode="ro")
        with pytest.raises(ACLViolation):
            enforce_read_acl(wild, "")
        with pytest.raises(ACLViolation):
            enforce_read_acl(wild, "   ")
        reg = make_registry(tmp_path, meta=stamp_meta({"acl": acl_block(readers=["*"])}, fp()))
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), reader_id="")
        with pytest.raises(ACLViolation):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0

    async def test_reader_whitespace_normalized(self, tmp_path):
        """Trivial registry whitespace in reader entries / reader_id is normalized, not a spurious denial."""
        # wildcard with trailing space still works; a padded reader entry still matches
        wild = read_instance_acl({"acl": acl_block(readers=["* "])})
        assert enforce_read_acl(wild, "anyone") is None
        padded = read_instance_acl({"acl": acl_block(readers=[" bobclaw "])})
        assert enforce_read_acl(padded, "bobclaw") is None
        assert enforce_read_acl(padded, " bobclaw ") is None  # reader_id stripped symmetrically
        # end-to-end: a padded reader entry permits the matching reader
        reg = make_registry(tmp_path, meta=stamp_meta({"acl": acl_block(readers=[" bobclaw "])}, fp()))
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = resp([point("a", 0.9, {})])
        ad = LKSReadAdapter(reg, client=client, live_slot=slot(), reader_id="bobclaw")
        assert [h.id for h in await ad.search("inst", query_vector=vec(), k=3)] == ["a"]

    async def test_malformed_embedder_result_fails_closed(self, tmp_path):
        """An embedder returning an empty / non-list result fails closed (ReadAdapterError), not IndexError/TypeError."""
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()

        class EmptyEmbedder:
            async def embed(self, texts):
                return []  # no vector

        class JunkEmbedder:
            async def embed(self, texts):
                return ["not-a-vector"]  # embedded[0] is not a list/tuple

        ad_empty = LKSReadAdapter(reg, client=client, embedder=EmptyEmbedder(), live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad_empty.search("inst", query="x", k=3)
        ad_junk = LKSReadAdapter(reg, client=client, embedder=JunkEmbedder(), live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad_junk.search("inst", query="x", k=3)
        assert client.query_points.call_count == 0

    async def test_filter_build_failure_fails_closed(self, tmp_path, monkeypatch):
        """A failure building the Qdrant filter surfaces as ReadAdapterError, not a raw exception."""
        import core.memory.lks_adapter as mod
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        client.collection_exists.return_value = True

        def _boom(_filters):
            raise ImportError("qdrant_client missing")

        monkeypatch.setattr(mod, "_build_filter", _boom)
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=3, filters={"source_path": "a.md"})

    async def test_invalid_filters_type_fails_closed(self, tmp_path):
        """A non-dict filters argument fails closed (ReadAdapterError), not an opaque AttributeError."""
        reg = make_registry(tmp_path, meta=full_meta())
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=3, filters="not-a-dict")
        assert client.query_points.call_count == 0

    async def test_garbled_instance_collection_fails_closed(self, tmp_path):
        """Defense-in-depth: a garbled ResolvedInstance.collection fails closed (ReadAdapterError)."""
        from core.ledger.federation import ResolvedInstance
        reg = make_registry(tmp_path, meta=full_meta())
        bad = ResolvedInstance(
            name="inst", repo="r", ledger_dir="ledger", collection=None, dim=DIM, meta=full_meta()
        )
        reg.resolve = lambda name: bad
        client = MagicMock()
        ad = LKSReadAdapter(reg, client=client, live_slot=slot())
        with pytest.raises(ReadAdapterError):
            await ad.search("inst", query_vector=vec(), k=3)
        assert client.query_points.call_count == 0
