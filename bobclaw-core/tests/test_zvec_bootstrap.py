from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import core.memory.bootstrap as bootstrap_mod
from core.ledger.federation import FederationRegistry
from core.memory.bootstrap import MemoryBootstrapConfig, bootstrap_memory, reset_memory
from core.memory.models import ChunkRecord
from core.memory.providers.zvec_provider import ZvecRetrievalProvider
from core.memory.write_fence import (
    WriteFence,
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
