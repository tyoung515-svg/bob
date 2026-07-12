from __future__ import annotations

import json
import os
import subprocess
import tomllib
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The zvec bootstrap path spawns the real storage child; the native `zvec`
# package is not a shipped dependency (opt-in zero-Docker provider), so this
# surface skips rather than failing a fresh contributor's suite.
pytest.importorskip(
    "zvec", reason="zvec not installed (optional zero-Docker provider: pip install zvec==0.5.1)"
)

import core.memory.bootstrap as bootstrap_mod
from core.ledger.federation import FederationRegistry
from core.memory.bootstrap import MemoryBootstrapConfig, bootstrap_memory, reset_memory
from core.memory.exceptions import MemoryConfigError
from core.memory.models import ChunkRecord, SlotResolution
from core.memory.providers.zvec_provider import ZvecRetrievalProvider
from core.memory.write_fence import (
    WriteFence,
    WriteFenceViolation,
    canonicalize_zvec_instance_root,
)


@pytest.fixture(autouse=True)
def _reset_bootstrap_globals():
    reset_memory()
    yield
    singletons = bootstrap_mod._bootstrap_singleton
    if singletons is not None:
        provider = getattr(getattr(singletons, "indexer", None), "_provider", None)
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        fence = getattr(singletons, "write_fence", None)
        if fence is not None:
            fence.close()
    reset_memory()


@pytest.fixture
def workspace_path() -> Path:
    root = (
        Path(__file__).resolve().parents[2]
        / "_workspace"
        / "testing"
        / "zvec-bootstrap-pytest"
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir()
    return path


def _toml_path(path: Path) -> str:
    return path.as_posix()


def _write_zvec_stores(path: Path, instance_root: Path) -> None:
    path.write_text(
        "[stores.test_store]\n"
        'acl_allowed_providers = ["zvec_local"]\n'
        "\n"
        "[providers.zvec_local]\n"
        'kind = "zvec"\n'
        'locality = "local"\n'
        f'instance_root = "{_toml_path(instance_root)}"\n'
        'collection_prefix = "zvec_test_"\n'
        'capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )


def _write_inline_zvec_stores(path: Path, instance_root: Path) -> None:
    path.write_text(
        "[stores]\n"
        'test_store = { acl_allowed_providers = ["zvec_local"] }\n'
        "\n"
        "[providers]\n"
        'zvec_local = { kind = "zvec", locality = "local", '
        f'instance_root = "{_toml_path(instance_root)}", '
        'collection_prefix = "zvec_test_", capability_classes = ["text_dense"] }\n',
        encoding="utf-8",
    )


def _config(workspace_path: Path, stores_path: Path) -> MemoryBootstrapConfig:
    return MemoryBootstrapConfig(
        enabled=True,
        sqlite_path=workspace_path / "configured-l0.sqlite",
        qdrant_url="http://localhost:16333",
        stores_config_path=stores_path,
        default_store_id="test_store",
    )


def test_zvec_selection_arms_fence_initializes_layout_and_stamps_compatible_fingerprint(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    instance_root = workspace_path / "zvec instance root"
    stores_path = workspace_path / "memory_stores.toml"
    _write_zvec_stores(stores_path, instance_root)
    monkeypatch.setenv(
        "BOBCLAW_LEDGER_INSTANCES", str(workspace_path / "ledger_instances.json")
    )
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "locks")
    )
    monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "true")

    with patch(
        "core.memory.bootstrap.QdrantClient",
        side_effect=AssertionError("zvec bootstrap must not construct QdrantClient"),
    ):
        first = bootstrap_memory(_config(workspace_path, stores_path))

    provider = first.indexer._provider
    assert isinstance(provider, ZvecRetrievalProvider)
    assert first.write_fence.lock_held is True
    assert first.write_fence.degraded is False
    assert first.write_fence.resource_identity == (
        f"{canonicalize_zvec_instance_root(instance_root)}|zvec_test_"
    )

    instance_dir = instance_root / "instances" / "test_store"
    manifest_dir = instance_dir / "manifest"
    fingerprint_path = manifest_dir / "embed_fingerprint.json"
    assert manifest_dir.is_dir()
    assert (instance_dir / "collections").is_dir()
    assert (instance_dir / "l0").is_dir()
    fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    assert fingerprint["embed"]["dim"] == 768

    provider.index(
        "test_store",
        [
            ChunkRecord(
                id="chunk:test-store:one",
                vector=[1.0] + [0.0] * 767,
                payload={"source_fact_id": "fact-1", "text": "real zvec boot"},
            )
        ],
    )
    assert (instance_dir / "collections" / "zvec_test__768").is_dir()

    provider.close()
    first.write_fence.close()
    reset_memory()

    with patch(
        "core.memory.bootstrap.QdrantClient",
        side_effect=AssertionError("zvec bootstrap must not construct QdrantClient"),
    ):
        second = bootstrap_memory(_config(workspace_path, stores_path))
    try:
        assert json.loads(fingerprint_path.read_text(encoding="utf-8")) == fingerprint
        assert isinstance(second.indexer._provider, ZvecRetrievalProvider)
    finally:
        second.indexer._provider.close()
        second.write_fence.close()
        reset_memory()


def test_inline_table_zvec_selection_never_contacts_qdrant(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    instance_root = workspace_path / "inline-zvec-root"
    stores_path = workspace_path / "inline-memory_stores.toml"
    _write_inline_zvec_stores(stores_path, instance_root)
    monkeypatch.setenv(
        "BOBCLAW_LEDGER_INSTANCES", str(workspace_path / "inline-registry.json")
    )
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "inline-locks")
    )

    with patch(
        "core.memory.bootstrap.QdrantClient",
        side_effect=AssertionError("inline-table zvec must not contact Qdrant"),
    ):
        singletons = bootstrap_memory(_config(workspace_path, stores_path))

    try:
        assert isinstance(singletons.indexer._provider, ZvecRetrievalProvider)
        assert singletons.write_fence.lock_held is True
        assert (instance_root / "instances" / "test_store" / "manifest").is_dir()
    finally:
        singletons.indexer._provider.close()
        singletons.write_fence.close()
        reset_memory()


def test_zvec_instance_root_spelling_maps_to_one_family_lock(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "locks")
    )
    instance_root = workspace_path / "one-root"
    holder = WriteFence(
        FederationRegistry(workspace_path / "holder-registry.json"),
        zvec_instance_root=instance_root,
        collection_prefix="zvec_test_",
    )
    contender = WriteFence(
        FederationRegistry(workspace_path / "contender-registry.json"),
        zvec_instance_root=instance_root / "." / ".." / "one-root",
        collection_prefix="zvec_test_",
    )
    try:
        assert canonicalize_zvec_instance_root(
            instance_root / "." / ".." / "one-root"
        ) == canonicalize_zvec_instance_root(instance_root)
        assert contender.lock_path == holder.lock_path
        assert contender.degraded is True
        assert contender.degraded_reason == "contention"
    finally:
        contender.close()
        holder.close()


def test_zvec_junction_alias_maps_to_target_family_lock(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "junction-locks")
    )
    target_root = workspace_path / "junction-target"
    alias_root = workspace_path / "junction-alias"
    target_root.mkdir()
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias_root), str(target_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    holder = WriteFence(
        FederationRegistry(workspace_path / "junction-holder-registry.json"),
        zvec_instance_root=target_root,
        collection_prefix="zvec_test_",
    )
    contender = WriteFence(
        FederationRegistry(workspace_path / "junction-contender-registry.json"),
        zvec_instance_root=alias_root,
        collection_prefix="zvec_test_",
    )
    try:
        assert canonicalize_zvec_instance_root(alias_root) == (
            canonicalize_zvec_instance_root(target_root)
        )
        assert contender.lock_path == holder.lock_path
        assert contender.degraded_reason == "contention"
    finally:
        contender.close()
        holder.close()


def test_foreign_collision_refusal_does_not_create_zvec_root(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "collision-locks")
    )
    registry = FederationRegistry(workspace_path / "collision-registry.json")
    registry.register(
        "foreign-owner",
        "C:/foreign",
        collection="zvec_test__768",
        dim=768,
    )
    instance_root = workspace_path / "must-not-exist"

    with pytest.raises(WriteFenceViolation, match="registry collision"):
        WriteFence(
            registry,
            zvec_instance_root=instance_root,
            collection_prefix="zvec_test_",
        )

    assert not instance_root.exists()


def test_absent_zvec_instance_root_case_variants_map_to_one_family_lock(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "locks")
    )
    roots = [
        workspace_path / "AbsentLockCase",
        workspace_path / "absentlockcase",
        workspace_path / "ABSENTLOCKCASE",
    ]
    assert all(not root.exists() for root in roots)

    fences = [
        WriteFence(
            FederationRegistry(workspace_path / f"registry-{index}.json"),
            zvec_instance_root=root,
            collection_prefix="zvec_test_",
        )
        for index, root in enumerate(roots)
    ]
    try:
        assert len({fence.lock_path for fence in fences}) == 1
        assert sum(fence.lock_held for fence in fences) == 1
        assert [fence.degraded_reason for fence in fences[1:]] == [
            "contention",
            "contention",
        ]
        assert roots[0].is_dir()
    finally:
        for fence in reversed(fences):
            fence.close()


def test_unc_zvec_instance_root_case_variants_map_to_one_family_lock(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "locks")
    )
    real_makedirs = os.makedirs

    def allow_test_unc_root(path, *args, **kwargs):
        if str(path).startswith("\\\\"):
            return None
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr("core.memory.write_fence.os.makedirs", allow_test_unc_root)
    roots = [
        r"\\TestServer\MemoryShare\AbsentLockCase",
        r"\\testserver\memoryshare\absentlockcase",
        r"\\TESTSERVER\MEMORYSHARE\ABSENTLOCKCASE",
    ]
    fences = [
        WriteFence(
            FederationRegistry(workspace_path / f"unc-registry-{index}.json"),
            zvec_instance_root=root,
            collection_prefix="zvec_test_",
        )
        for index, root in enumerate(roots)
    ]
    try:
        assert len({fence.lock_path for fence in fences}) == 1
        assert sum(fence.lock_held for fence in fences) == 1
        assert [fence.degraded_reason for fence in fences[1:]] == [
            "contention",
            "contention",
        ]
    finally:
        for fence in reversed(fences):
            fence.close()


def test_zvec_second_boot_refuses_mismatched_fingerprint(
    workspace_path: Path, monkeypatch: pytest.MonkeyPatch
):
    instance_root = workspace_path / "mismatch-root"
    stores_path = workspace_path / "memory_stores.toml"
    _write_zvec_stores(stores_path, instance_root)
    monkeypatch.setenv(
        "BOBCLAW_LEDGER_INSTANCES", str(workspace_path / "ledger_instances.json")
    )
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / "locks")
    )

    first = bootstrap_memory(_config(workspace_path, stores_path))
    fingerprint_path = (
        instance_root
        / "instances"
        / "test_store"
        / "manifest"
        / "embed_fingerprint.json"
    )
    first.indexer._provider.close()
    first.write_fence.close()
    reset_memory()

    mismatched = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    mismatched["embed"]["model_id"] = "different-768-model"
    fingerprint_path.write_text(
        json.dumps(mismatched, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with (
        patch(
            "core.memory.bootstrap.QdrantClient",
            side_effect=AssertionError("zvec bootstrap must not construct QdrantClient"),
        ),
        pytest.raises(MemoryConfigError, match="fingerprint mismatch"),
    ):
        bootstrap_memory(_config(workspace_path, stores_path))
    assert json.loads(fingerprint_path.read_text(encoding="utf-8")) == mismatched


def test_zvec_pure_fence_loss_removes_this_call_stamp(
    workspace_path: Path,
):
    instance_root = workspace_path / "pure-fence-loss-root"
    fingerprint_path = (
        instance_root
        / "instances"
        / "test_store"
        / "manifest"
        / "embed_fingerprint.json"
    )

    class LostFence:
        def __init__(self):
            self.calls = 0
            self.held = True

        def assert_writable(self, collection):
            self.calls += 1
            if not self.held:
                raise WriteFenceViolation(collection, "pure test fence loss")
            if self.calls == 2:
                self.held = False

    slot_resolver = MagicMock()
    slot_resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-embedder",
        backend="openai_compatible",
        endpoint="http://127.0.0.1:1/v1",
        embedding_dimension=768,
    )
    fence = LostFence()

    with pytest.raises(WriteFenceViolation, match="pure test fence loss"):
        bootstrap_mod._initialize_zvec_instance(
            fence,
            slot_resolver,
            instance_root,
            "test_store",
            "zvec_test_",
        )

    assert fence.calls == 3
    assert not fingerprint_path.exists()


def test_zvec_fingerprint_loss_leaves_identical_successor_stamp(
    workspace_path: Path,
):
    instance_root = workspace_path / "successor-stamp-root"
    fingerprint_path = (
        instance_root
        / "instances"
        / "test_store"
        / "manifest"
        / "embed_fingerprint.json"
    )

    class SuccessorFence:
        def __init__(self):
            self.calls = 0
            self.held = True
            self.successor_stat = None

        def assert_writable(self, collection):
            self.calls += 1
            if self.calls == 3:
                written = fingerprint_path.read_bytes()
                successor = fingerprint_path.with_suffix(".successor")
                successor.write_bytes(written)
                self.held = False
                os.replace(successor, fingerprint_path)
                self.successor_stat = fingerprint_path.stat()
                raise WriteFenceViolation(collection, "successor acquired the fence")
            if not self.held:
                raise WriteFenceViolation(collection, "test fence was released")

    slot_resolver = MagicMock()
    slot_resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-embedder",
        backend="openai_compatible",
        endpoint="http://127.0.0.1:1/v1",
        embedding_dimension=768,
    )
    fence = SuccessorFence()

    with pytest.raises(WriteFenceViolation, match="successor acquired the fence"):
        bootstrap_mod._initialize_zvec_instance(
            fence,
            slot_resolver,
            instance_root,
            "test_store",
            "zvec_test_",
        )

    assert fence.calls == 3
    assert fingerprint_path.exists()
    assert os.path.samestat(fingerprint_path.stat(), fence.successor_stat)
    assert json.loads(fingerprint_path.read_text(encoding="utf-8"))["embed"][
        "model_id"
    ] == "test-embedder"


def test_zvec_fingerprint_rollback_removes_owned_stamp_while_fence_is_held(
    workspace_path: Path,
):
    instance_root = workspace_path / "owned-rollback-root"
    fingerprint_path = (
        instance_root
        / "instances"
        / "test_store"
        / "manifest"
        / "embed_fingerprint.json"
    )

    class HeldFence:
        def __init__(self):
            self.calls = 0

        def assert_writable(self, collection):
            self.calls += 1
            if self.calls == 3:
                raise WriteFenceViolation(collection, "synthetic post-check failure")

    slot_resolver = MagicMock()
    slot_resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-embedder",
        backend="openai_compatible",
        endpoint="http://127.0.0.1:1/v1",
        embedding_dimension=768,
    )
    fence = HeldFence()

    with pytest.raises(WriteFenceViolation, match="synthetic post-check failure"):
        bootstrap_mod._initialize_zvec_instance(
            fence,
            slot_resolver,
            instance_root,
            "test_store",
            "zvec_test_",
        )

    assert fence.calls == 3
    assert not fingerprint_path.exists()


def test_config_without_zvec_keeps_the_legacy_qdrant_construction_path(
    workspace_path: Path,
):
    stores_path = workspace_path / "legacy-memory_stores.toml"
    stores_path.write_text(
        "[stores.test_store]\n"
        'acl_allowed_providers = ["test_provider"]\n'
        "\n"
        "[providers.test_provider]\n"
        'locality = "local"\n'
        'collection_prefix = "test_"\n'
        'capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    client = MagicMock(name="qdrant_client")
    client.get_collections.return_value = MagicMock()
    fence = MagicMock(name="write_fence")

    with (
        patch("core.memory.bootstrap.QdrantClient", return_value=client) as qdrant_cls,
        patch.object(
            bootstrap_mod, "_maybe_build_write_fence", return_value=fence
        ) as fence_builder,
        patch.object(bootstrap_mod, "QdrantRetrievalProvider") as qdrant_provider,
        patch.object(bootstrap_mod, "ZvecRetrievalProvider") as zvec_provider,
    ):
        singletons = bootstrap_memory(_config(workspace_path, stores_path))

    qdrant_cls.assert_called_once_with(url="http://localhost:16333", timeout=10)
    client.get_collections.assert_called_once_with()
    fence_builder.assert_called_once_with(
        singletons.slot_resolver,
        "test_",
        "http://localhost:16333",
    )
    qdrant_provider.assert_called_once_with(
        provider_id="test_provider",
        locality="local",
        collection_prefix="test_",
        acl_registry=singletons.acl_registry,
        client=client,
        write_fence=fence,
    )
    zvec_provider.assert_not_called()


@pytest.mark.parametrize(
    ("case", "qdrant_fails", "expected_exception"),
    [
        ("valid", False, None),
        ("malformed", False, tomllib.TOMLDecodeError),
        ("missing", False, FileNotFoundError),
        ("malformed", True, MemoryConfigError),
        ("missing", True, MemoryConfigError),
    ],
)
def test_legacy_single_parse_equivalence_corpus(
    workspace_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    qdrant_fails: bool,
    expected_exception: type[BaseException] | None,
):
    stores_path = workspace_path / f"{case}-legacy-memory_stores.toml"
    if case == "valid":
        stores_path.write_text(
            "[stores.test_store]\n"
            'acl_allowed_providers = ["test_provider"]\n'
            "\n"
            "[providers.test_provider]\n"
            'locality = "local"\n'
            'collection_prefix = "test_"\n'
            'capability_classes = ["text_dense"]\n',
            encoding="utf-8",
        )
    elif case == "malformed":
        stores_path.write_text("[providers.test_provider\n", encoding="utf-8")

    monkeypatch.setenv(
        "BOBCLAW_LEDGER_INSTANCES", str(workspace_path / f"{case}-registry.json")
    )
    monkeypatch.setenv(
        "BOBCLAW_WRITE_FENCE_LOCK_DIR", str(workspace_path / f"{case}-locks")
    )
    reads = []
    real_read_text = Path.read_text

    def track_stores_read(self, *args, **kwargs):
        if self == stores_path:
            reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_stores_read)
    client = MagicMock(name="qdrant_client")
    client.get_collections.return_value = MagicMock()
    fence = MagicMock(name="write_fence")
    qdrant_error = RuntimeError("base-order qdrant failure")
    qdrant_side_effect = qdrant_error if qdrant_fails else None

    context = (
        pytest.raises(expected_exception)
        if expected_exception is not None
        else nullcontext()
    )
    with (
        patch(
            "core.memory.bootstrap.QdrantClient",
            return_value=client,
            side_effect=qdrant_side_effect,
        ) as qdrant_cls,
        patch.object(
            bootstrap_mod, "_maybe_build_write_fence", return_value=fence
        ),
        patch.object(
            bootstrap_mod, "QdrantRetrievalProvider"
        ) as qdrant_provider,
        context as captured,
    ):
        bootstrap_memory(_config(workspace_path, stores_path))

    assert len(reads) == 1
    assert _config(workspace_path, stores_path).sqlite_path.is_file()
    qdrant_cls.assert_called_once_with(url="http://localhost:16333", timeout=10)
    if qdrant_fails:
        assert "Qdrant unreachable" in str(captured.value)
    else:
        client.get_collections.assert_called_once_with()
    if case == "valid":
        qdrant_provider.assert_called_once()
        provider_kwargs = qdrant_provider.call_args.kwargs
        assert provider_kwargs["provider_id"] == "test_provider"
        assert provider_kwargs["locality"] == "local"
        assert provider_kwargs["collection_prefix"] == "test_"
        assert provider_kwargs["client"] is client
        assert provider_kwargs["write_fence"] is fence
    else:
        qdrant_provider.assert_not_called()


def test_non_table_provider_block_raises_named_memory_config_error(
    workspace_path: Path,
):
    stores_path = workspace_path / "bad-provider-memory_stores.toml"
    stores_path.write_text(
        "[stores.test_store]\n"
        'acl_allowed_providers = ["bad_provider"]\n'
        "\n"
        "[providers]\n"
        'bad_provider = "not-a-table"\n',
        encoding="utf-8",
    )
    client = MagicMock(name="qdrant_client")
    client.get_collections.return_value = MagicMock()

    with (
        patch("core.memory.bootstrap.QdrantClient", return_value=client),
        pytest.raises(MemoryConfigError, match="bad_provider"),
    ):
        bootstrap_memory(_config(workspace_path, stores_path))
