import asyncio
from pathlib import Path
import types
from unittest.mock import MagicMock

import pytest

from core.memory.write_fence import (
    WriteFence,
    WriteFenceViolation,
    enforce_write_acl,
    register_bobclaw_memory,
    backfill_corpus_acl,
    BOBCLAW_MEMORY_INSTANCE,
    BOBCLAW_MEMORY_COLLECTION,
)
from core.memory.exceptions import ACLViolation
from core.memory.fingerprint import EmbedFingerprint
from core.memory.lks_adapter import InstanceACL, read_instance_acl, LKSReadAdapter
from core.memory.models import ChunkRecord
from core.ledger.federation import FederationRegistry, FederationError
from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
from core.memory.acl import ACLRegistry

# ---------------------------------------------------------------------------
# Helpers (from spec)
# ---------------------------------------------------------------------------
DIM = 768


def fp(model="model-a", dim=DIM):
    return EmbedFingerprint(model, dim, True, "cosine")


def reg_with_bob(tmp_path):
    reg = FederationRegistry(tmp_path / "reg.json")
    register_bobclaw_memory(reg, fp())
    return reg


def add_instance(reg, name, collection, *, meta):
    reg.register(name, "/repos/_t", collection=collection, dim=DIM, meta=meta)


def permissive_acl_registry(tmp_path):
    f = tmp_path / "stores.toml"
    f.write_text(
        '[store.s]\nallowed_locality = ["local"]\n'
        'allowed_provider_ids = ["p"]\n'
        'allowed_capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    return ACLRegistry(f)


def vec768(seed=0.1):
    return [seed] + [0.0] * (DIM - 1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWriteFence:

    # 1
    def test_register_bobclaw_memory(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        rec = register_bobclaw_memory(reg, fp())
        assert rec["collection"] == BOBCLAW_MEMORY_COLLECTION == "bobclaw__768"
        assert rec["dim"] == 768
        assert rec["meta"]["acl"]["writer"] == "bobclaw"
        assert rec["meta"]["acl"]["mode"] == "rw"
        assert rec["meta"]["embed"] == fp().to_dict()
        assert rec["name"] == BOBCLAW_MEMORY_INSTANCE == "bobclaw-memory"

    # 2
    def test_collection_uniqueness_and_dup_name(self, tmp_path: Path):
        reg = reg_with_bob(tmp_path)
        # duplicate collection
        with pytest.raises(FederationError):
            reg.register("other", "C:/d", collection="bobclaw__768", dim=768)
        # duplicate name without overwrite
        with pytest.raises(FederationError):
            register_bobclaw_memory(reg, fp())
        # overwrite works
        rec = register_bobclaw_memory(reg, fp(), overwrite=True)
        assert rec["name"] == "bobclaw-memory"

    # 3
    def test_backfill_preserves_meta(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        reg.register(
            "wiki", "/repos/wiki", collection="wiki_chunks", dim=768,
            meta={"note": "keep me"}
        )
        backfill_corpus_acl(reg, ["wiki"])
        m = reg.get("wiki")["meta"]
        assert m["note"] == "keep me"
        assert m["acl"]["writer"] == "lks"
        assert m["acl"]["mode"] == "ro"
        assert "bobclaw" in m["acl"]["readers"]

    # 4
    def test_owned_write_allowed(self, tmp_path: Path):
        reg = reg_with_bob(tmp_path)
        assert WriteFence(reg).assert_writable("bobclaw__768") is None

    # 5
    def test_cross_write_refused(self, tmp_path: Path):
        reg = reg_with_bob(tmp_path)
        add_instance(
            reg, "wiki", "wiki_chunks",
            meta={"acl": {"writer": "lks", "readers": ["bobclaw"], "mode": "ro"}},
        )
        with pytest.raises(WriteFenceViolation):
            WriteFence(reg).assert_writable("wiki_chunks")

    # 6
    def test_unregistered_refused_failclosed(self, tmp_path: Path):
        reg = reg_with_bob(tmp_path)
        with pytest.raises(WriteFenceViolation):
            WriteFence(reg).assert_writable("never_registered_999")

    # 7
    def test_no_acl_refused_failclosed(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        add_instance(reg, "x", "c_x", meta={"note": "no acl here"})
        with pytest.raises(WriteFenceViolation):
            WriteFence(reg).assert_writable("c_x")

    # 8
    def test_garbled_acl_refused_failclosed(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        add_instance(reg, "g1", "c_g1", meta={"acl": "not-a-dict"})
        add_instance(reg, "g2", "c_g2", meta={"acl": {"mode": "rw"}})  # readers missing
        for coll in ("c_g1", "c_g2"):
            # audit r1 (accepted): the refusal type is UNIFORM — a garbled ACL raises WriteFenceViolation
            # (which IS an ACLViolation, so base-class catchers still work).
            with pytest.raises(WriteFenceViolation):
                WriteFence(reg).assert_writable(coll)

    # 8b — audit r1 (rejected finding A, pinned as an invariant): the registered bobclaw-memory collection
    # equals the provider's computed collection for the prod prefix, so the fence ALLOWS BoB's real write
    # (no false-positive regression). `_collection_name(768)` = f"{'bobclaw_'}_{768}" = "bobclaw__768" (double _).
    def test_registered_collection_matches_provider_name(self, tmp_path: Path):
        prov = QdrantRetrievalProvider(
            provider_id="p", locality="local", collection_prefix="bobclaw_",
            acl_registry=permissive_acl_registry(tmp_path), client=MagicMock(),
        )
        assert prov._collection_name(768) == BOBCLAW_MEMORY_COLLECTION == "bobclaw__768"
        reg = reg_with_bob(tmp_path)
        assert WriteFence(reg).assert_writable(prov._collection_name(768)) is None  # owned -> allowed

    # 9
    def test_non_write_mode_refused(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        add_instance(
            reg, "roi", "c_ro",
            meta={"acl": {"writer": "bobclaw", "readers": ["bobclaw"], "mode": "ro"}},
        )
        with pytest.raises(WriteFenceViolation):
            WriteFence(reg).assert_writable("c_ro")
        # enforce_write_acl directly
        acl = read_instance_acl(
            {"acl": {"writer": "bobclaw", "readers": ["bobclaw"], "mode": "ro"}}
        )
        with pytest.raises(WriteFenceViolation):
            enforce_write_acl(acl, "bobclaw")

    # 10
    def test_blank_collection_refused(self, tmp_path: Path):
        reg = reg_with_bob(tmp_path)
        for bad in ("", "   "):
            with pytest.raises(WriteFenceViolation):
                WriteFence(reg).assert_writable(bad)

    # 11
    def test_provider_seam_cross_write_refused_no_mutation(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        add_instance(
            reg, "lksinst", "_t_lks_768",
            meta={"acl": {"writer": "lks", "readers": ["bobclaw"], "mode": "ro"}},
        )
        fence = WriteFence(reg)
        client = MagicMock()
        prov = QdrantRetrievalProvider(
            provider_id="p",
            locality="local",
            collection_prefix="_t_lks",
            acl_registry=permissive_acl_registry(tmp_path),
            client=client,
            write_fence=fence,
        )
        with pytest.raises(WriteFenceViolation):
            prov.index(
                "s",
                [ChunkRecord(id="c1", vector=vec768(), payload={})],
            )
        assert client.create_collection.call_count == 0
        assert client.upsert.call_count == 0

    # 12
    def test_provider_seam_owned_write_proceeds(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        register_bobclaw_memory(reg, fp(), collection="_t_own_768")
        fence = WriteFence(reg)
        client = MagicMock()
        client.get_collection.return_value = MagicMock()  # exists
        prov = QdrantRetrievalProvider(
            provider_id="p",
            locality="local",
            collection_prefix="_t_own",
            acl_registry=permissive_acl_registry(tmp_path),
            client=client,
            write_fence=fence,
        )
        rec = prov.index(
            "s",
            [ChunkRecord(id="c1", vector=vec768(), payload={"text": "x"})],
        )
        assert rec.item_count == 1
        client.upsert.assert_called_once()
        assert client.upsert.call_args.kwargs["collection_name"] == "_t_own_768"

    # 13
    def test_provider_none_fence_byte_identical(self, tmp_path: Path):
        client = MagicMock()
        client.get_collection.return_value = MagicMock()  # exists
        prov = QdrantRetrievalProvider(
            provider_id="p",
            locality="local",
            collection_prefix="_t_own",
            acl_registry=permissive_acl_registry(tmp_path),
            client=client,
        )  # NO write_fence
        prov.index(
            "s",
            [ChunkRecord(id="c1", vector=vec768(), payload={})],
        )
        client.upsert.assert_called_once()

    # 14
    def test_writefenceviolation_is_aclviolation(self):
        assert issubclass(WriteFenceViolation, ACLViolation)

    # 15
    def test_backfilled_instance_reads_under_require_acl_true(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        reg.register(
            "wiki", "/repos/wiki", collection="wiki_chunks", dim=768,
            meta={"note": "n"},
        )
        backfill_corpus_acl(reg, ["wiki"])
        # reader allowed
        client = MagicMock()
        client.collection_exists.return_value = True
        client.query_points.return_value = types.SimpleNamespace(points=[])
        ad = LKSReadAdapter(
            reg,
            client=client,
            require_acl=True,
            require_stamp=False,
            reader_id="bobclaw",
        )
        hits = asyncio.run(ad.search("wiki", query_vector=vec768(), k=3))
        assert hits == []
        # intruder denied
        ad2 = LKSReadAdapter(
            reg,
            client=client,
            require_acl=True,
            require_stamp=False,
            reader_id="intruder",
        )
        with pytest.raises(ACLViolation):
            asyncio.run(ad2.search("wiki", query_vector=vec768(), k=3))

    # 16 — audit r2 (rejected): a None/missing meta does NOT crash with AttributeError —
    # read_instance_acl(None) returns None, so the fence refuses (fail-closed) via the no-acl branch.
    def test_meta_none_or_missing_refused_failclosed(self, tmp_path: Path):
        # (a) a registry hand-loaded with an explicit null meta
        p = tmp_path / "rnull.json"
        p.write_text(
            '{"version": 1, "instances": {"n": {"repo": "C:/d", "ledger_dir": "ledger",'
            ' "collection": "c_null", "dim": 768, "meta": null}}}',
            encoding="utf-8",
        )
        reg = FederationRegistry(p).load()
        with pytest.raises(WriteFenceViolation):
            WriteFence(reg).assert_writable("c_null")
        # (b) the default meta ({} from register) — no acl key — also refuses
        reg2 = FederationRegistry(tmp_path / "r2.json")
        reg2.register("m", "C:/d", collection="c_empty", dim=768)  # meta defaults to {}
        with pytest.raises(WriteFenceViolation):
            WriteFence(reg2).assert_writable("c_empty")

    # 17 — audit r2 (accepted): a multi-dim index is fail-closed ATOMIC — one non-owned dim aborts the
    # whole index BEFORE any create/upsert (no partial write across collections).
    def test_provider_multidim_index_atomic_failclosed(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        register_bobclaw_memory(reg, fp(), collection="_t_own_768")  # dim-768 coll owned
        # "_t_own_3" (dim-3 coll) is left UNREGISTERED → fence refuses it
        fence = WriteFence(reg)
        client = MagicMock()
        client.get_collection.return_value = MagicMock()
        prov = QdrantRetrievalProvider(
            provider_id="p", locality="local", collection_prefix="_t_own",
            acl_registry=permissive_acl_registry(tmp_path), client=client, write_fence=fence,
        )
        items = [
            ChunkRecord(id="ok", vector=vec768(), payload={}),       # -> _t_own_768 (owned)
            ChunkRecord(id="bad", vector=[0.1, 0.2, 0.3], payload={}),  # -> _t_own_3 (unregistered)
        ]
        with pytest.raises(WriteFenceViolation):
            prov.index("s", items)
        # ATOMIC: neither collection was created or upserted
        assert client.create_collection.call_count == 0
        assert client.upsert.call_count == 0

    # 18 — audit r2 (accepted, delete-path coverage): the delete guard refuses a non-owned collection
    # before any client.delete (no mutation), and allows an owned one.
    def test_provider_delete_fence_refused_no_mutation(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        add_instance(
            reg, "lksinst", "_t_lks_768",
            meta={"acl": {"writer": "lks", "readers": ["bobclaw"], "mode": "ro"}},
        )
        client = MagicMock()
        client.get_collections.return_value = types.SimpleNamespace(
            collections=[types.SimpleNamespace(name="_t_lks_768")]
        )
        prov = QdrantRetrievalProvider(
            provider_id="p", locality="local", collection_prefix="_t_lks",
            acl_registry=permissive_acl_registry(tmp_path), client=client, write_fence=WriteFence(reg),
        )
        with pytest.raises(WriteFenceViolation):
            prov.delete("s", ["id1"])
        assert client.delete.call_count == 0

    def test_provider_delete_fence_owned_allowed(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        register_bobclaw_memory(reg, fp(), collection="_t_own_768")
        client = MagicMock()
        client.get_collections.return_value = types.SimpleNamespace(
            collections=[types.SimpleNamespace(name="_t_own_768")]
        )
        prov = QdrantRetrievalProvider(
            provider_id="p", locality="local", collection_prefix="_t_own",
            acl_registry=permissive_acl_registry(tmp_path), client=client, write_fence=WriteFence(reg),
        )
        prov.delete("s", ["id1"])
        client.delete.assert_called_once()
        assert client.delete.call_args.kwargs["collection_name"] == "_t_own_768"

    # 19 — audit r3 (accepted): the bootstrap fence derives the registered collection from the SAME
    # configured collection_prefix the provider uses (f"{prefix}_{dim}"), so a non-default prefix can
    # never cause a false-positive denial. Default-off flag returns None (byte-identical).
    def test_bootstrap_fence_derives_collection_from_prefix(self, tmp_path: Path, monkeypatch):
        from core.memory.bootstrap import _maybe_build_write_fence
        from core.memory.models import SlotResolution

        stub = types.SimpleNamespace(
            get=lambda n: SlotResolution(
                slot_name="embed_text", model="m", backend="b", endpoint="e",
                embedding_dimension=768,
            )
        )
        # flag OFF -> None (default, byte-identical)
        monkeypatch.delenv("MEMORY_WRITE_FENCE_ENABLED", raising=False)
        assert _maybe_build_write_fence(stub, "bobclaw_") is None
        # flag ON + a CUSTOM prefix -> registers f"{prefix}_768" and the fence allows exactly that
        monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "true")
        monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(tmp_path / "reg.json"))
        fence = _maybe_build_write_fence(stub, "custom_prefix_")
        assert fence is not None
        assert fence.assert_writable("custom_prefix__768") is None  # owned -> allowed
        with pytest.raises(WriteFenceViolation):
            fence.assert_writable("bobclaw__768")  # not registered under this prefix

    # 20 — audit r4 (accepted): a STALE existing bobclaw-memory registration (prior dim / changed
    # prefix / hand-edit) is RECONCILED to the live prefix-derived collection, so it can never
    # false-positive-deny the provider's real write.
    def test_bootstrap_fence_reconciles_stale_registration(self, tmp_path: Path, monkeypatch):
        from core.memory.bootstrap import _maybe_build_write_fence
        from core.memory.models import SlotResolution

        # pre-persist a registry whose bobclaw-memory points at a STALE collection
        regpath = tmp_path / "reg.json"
        reg0 = FederationRegistry(regpath)
        register_bobclaw_memory(reg0, fp(), collection="bobclaw__768")
        reg0.save()

        stub = types.SimpleNamespace(
            get=lambda n: SlotResolution(
                slot_name="embed_text", model="m", backend="b", endpoint="e",
                embedding_dimension=768,
            )
        )
        monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "true")
        monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(regpath))
        fence = _maybe_build_write_fence(stub, "custom_prefix_")
        # reconciled to the CURRENT prefix-derived collection (the provider's real write target)
        assert fence.assert_writable("custom_prefix__768") is None
        # the stale collection is no longer the owned one
        with pytest.raises(WriteFenceViolation):
            fence.assert_writable("bobclaw__768")

    # 21 — audit r5 (accepted): trivial registry whitespace on the writer/mode must NOT spuriously deny a
    # legitimate owned write (enforce_write_acl normalizes both sides of the comparison).
    def test_spaced_writer_and_mode_owned_write_allowed(self, tmp_path: Path):
        reg = FederationRegistry(tmp_path / "r.json")
        add_instance(
            reg, "spaced", "c_spaced",
            meta={"acl": {"writer": "  bobclaw  ", "readers": ["bobclaw"], "mode": "  rw  "}},
        )
        assert WriteFence(reg, owner="bobclaw").assert_writable("c_spaced") is None
        # enforce_write_acl directly with spaced values
        acl = read_instance_acl({"acl": {"writer": " bobclaw ", "readers": ["bobclaw"], "mode": " rw "}})
        assert enforce_write_acl(acl, " bobclaw ") is None

    # 22 — audit r5 (accepted, defensive fail-closed): an unexpected non-dict meta type that makes
    # read_instance_acl raise (e.g. TypeError) is converted to a WriteFenceViolation, never escapes.
    def test_unparseable_meta_type_refused_failclosed(self):
        stub_registry = types.SimpleNamespace(by_collection=lambda coll: {"meta": 42})
        with pytest.raises(WriteFenceViolation):
            WriteFence(stub_registry).assert_writable("anything")

    # 23 — audit r5 (accepted): a corrupted WRITER on an existing bobclaw-memory registration (right
    # collection, wrong writer) is RECONCILED on bootstrap, so it can't false-positive-deny the owned write.
    def test_bootstrap_fence_reconciles_stale_writer(self, tmp_path: Path, monkeypatch):
        from core.memory.bootstrap import _maybe_build_write_fence
        from core.memory.models import SlotResolution

        regpath = tmp_path / "reg.json"
        reg0 = FederationRegistry(regpath)
        # bobclaw-memory exists with the RIGHT collection but a CORRUPTED writer ("lks")
        reg0.register(
            "bobclaw-memory", "C:/d", collection="bobclaw__768", dim=768,
            meta={"acl": {"writer": "lks", "readers": ["bobclaw"], "mode": "rw"},
                  "embed": fp().to_dict()},
        )
        reg0.save()

        stub = types.SimpleNamespace(
            get=lambda n: SlotResolution(
                slot_name="embed_text", model="m", backend="b", endpoint="e",
                embedding_dimension=768,
            )
        )
        monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "true")
        monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(regpath))
        fence = _maybe_build_write_fence(stub, "bobclaw_")
        # reconciled to writer=bobclaw -> the owned write is allowed
        assert fence.assert_writable("bobclaw__768") is None
