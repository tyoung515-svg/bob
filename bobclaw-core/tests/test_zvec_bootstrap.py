from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def test_zvec_fingerprint_stamp_is_invalidated_when_fence_is_lost_mid_write(
    workspace_path: Path,
):
    class SelfReleasingFence:
        def __init__(self):
            self.calls = 0
            self.held = True

        def assert_writable(self, collection):
            self.calls += 1
            if not self.held:
                raise WriteFenceViolation(collection, "test fence was released")
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
    instance_root = workspace_path / "lost-fence-root"
    fence = SelfReleasingFence()

    with pytest.raises(WriteFenceViolation, match="test fence was released"):
        bootstrap_mod._initialize_zvec_instance(
            fence,
            slot_resolver,
            instance_root,
            "test_store",
            "zvec_test_",
        )

    fingerprint_path = (
        instance_root
        / "instances"
        / "test_store"
        / "manifest"
        / "embed_fingerprint.json"
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


def test_config_without_zvec_constructs_qdrant_before_store_parsing(
    workspace_path: Path,
):
    stores_path = workspace_path / "legacy-order-memory_stores.toml"
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
    order = []
    client = MagicMock(name="qdrant_client")
    client.get_collections.return_value = MagicMock()
    real_parse = bootstrap_mod._parse_stores_toml

    def construct_qdrant(*args, **kwargs):
        order.append("qdrant")
        return client

    def parse_stores(path):
        order.append("parse")
        return real_parse(path)

    with (
        patch("core.memory.bootstrap.QdrantClient", side_effect=construct_qdrant),
        patch.object(bootstrap_mod, "_parse_stores_toml", side_effect=parse_stores),
        patch.object(
            bootstrap_mod, "_maybe_build_write_fence", return_value=MagicMock()
        ),
        patch.object(bootstrap_mod, "QdrantRetrievalProvider"),
    ):
        bootstrap_memory(_config(workspace_path, stores_path))

    assert order == ["qdrant", "parse"]


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
