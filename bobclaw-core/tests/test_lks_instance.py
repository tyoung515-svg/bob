from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

import pytest

from core.ledger.federation import FederationRegistry
from core.memory.acl import ACLRegistry
from core.memory.models import SlotResolution
from core.memory.providers.zvec_provider import ZvecRetrievalProvider
from core.memory.write_fence import WriteFence


class _KeywordEmbedder:
    embedding_dimension = 3

    async def embed_doc(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = text.lower()
        if "zvec" in normalized or "vector store" in normalized:
            return [1.0, 0.0, 0.0]
        if "solar" in normalized or "battery" in normalized:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


@pytest.fixture
def workspace_path() -> Path:
    root = (
        Path(__file__).resolve().parents[2]
        / "_workspace"
        / "testing"
        / "boblks-pytest"
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir()
    return path


def _acl(workspace_path: Path) -> ACLRegistry:
    stores_path = workspace_path / "memory_stores.toml"
    stores_path.write_text(
        "[store.bob_lks]\n"
        'allowed_locality = ["local"]\n'
        'allowed_provider_ids = ["zvec-local"]\n'
        'allowed_capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    return ACLRegistry(stores_path)


def _slots() -> SimpleNamespace:
    resolution = SlotResolution(
        slot_name="embed_text",
        model="test-embedder",
        backend="test",
        endpoint="test://embedder",
        embedding_dimension=3,
    )
    return SimpleNamespace(get=lambda _: resolution)


def _fence(workspace_path: Path, instance_root: Path) -> WriteFence:
    return WriteFence(
        FederationRegistry(workspace_path / "registry.json"),
        zvec_instance_root=instance_root,
        collection_prefix="bob_lks_",
        lock_dir=workspace_path / "locks",
    )


def _provider(
    workspace_path: Path,
    instance_root: Path,
    fence: WriteFence,
) -> ZvecRetrievalProvider:
    return ZvecRetrievalProvider(
        provider_id="zvec-local",
        locality="local",
        collection_prefix="bob_lks_",
        acl_registry=_acl(workspace_path),
        store_root=instance_root,
        write_fence=fence,
    )


def _build_lks(
    instance_root: Path,
    fence: WriteFence,
    provider: ZvecRetrievalProvider,
):
    from core.lks.instance import BobLKS

    return BobLKS(
        provider=provider,
        embedder=_KeywordEmbedder(),
        slot_resolver=_slots(),
        write_fence=fence,
        instance_root=instance_root,
        store_id="bob_lks",
        collection_prefix="bob_lks_",
    )


def _write_documents(workspace_path: Path) -> list[Path]:
    docs = workspace_path / "documents"
    docs.mkdir()
    first = docs / "local-store.md"
    first.write_text(
        "# Local Store\n\n"
        "Zvec keeps the local vector store on disk for durable retrieval.\n",
        encoding="utf-8",
    )
    second = docs / "solar.md"
    second.write_text(
        "# Solar\n\n"
        "Solar battery health needs a daily voltage review.\n",
        encoding="utf-8",
    )
    third = docs / "other.md"
    third.write_text(
        "# Other\n\n"
        "A third document covers release planning and maintenance windows.\n",
        encoding="utf-8",
    )
    return [first, second, third]


@pytest.mark.asyncio
async def test_first_boot_ingests_documents_and_retrieves_relevant_chunk(
    workspace_path: Path,
):
    instance_root = workspace_path / "zvec"
    fence = _fence(workspace_path, instance_root)
    provider = _provider(workspace_path, instance_root, fence)
    try:
        lks = _build_lks(instance_root, fence, provider)
        instance_dir = instance_root / "instances" / "bob_lks"
        assert (instance_dir / "manifest").is_dir()
        assert (instance_dir / "collections").is_dir()
        assert (instance_dir / "l0").is_dir()

        await lks.ingest(_write_documents(workspace_path))
        results = await lks.retrieve("zvec local vector store", 2)

        assert results.hits
        assert "Zvec keeps the local vector store" in results.hits[0].payload["text"]
    finally:
        provider.close()
        fence.close()


@pytest.mark.asyncio
async def test_reingesting_unchanged_document_does_not_write(
    workspace_path: Path,
):
    instance_root = workspace_path / "zvec"
    fence = _fence(workspace_path, instance_root)
    provider = _provider(workspace_path, instance_root, fence)
    try:
        lks = _build_lks(instance_root, fence, provider)
        documents = _write_documents(workspace_path)
        provider.index = MagicMock(wraps=provider.index)
        provider.delete = MagicMock(wraps=provider.delete)

        await lks.ingest(documents)
        index_calls = provider.index.call_count
        delete_calls = provider.delete.call_count

        await lks.ingest(documents)

        assert provider.index.call_count == index_calls
        assert provider.delete.call_count == delete_calls
    finally:
        provider.close()
        fence.close()


@pytest.mark.asyncio
async def test_changed_document_deletes_prior_source_chunks_then_reindexes(
    workspace_path: Path,
):
    instance_root = workspace_path / "zvec"
    fence = _fence(workspace_path, instance_root)
    provider = _provider(workspace_path, instance_root, fence)
    try:
        lks = _build_lks(instance_root, fence, provider)
        documents = _write_documents(workspace_path)
        await lks.ingest(documents)

        source_doc_id = documents[0].resolve().as_posix()
        original_ids = list(
            provider.scroll_payload("bob_lks", {"source_fact_id": source_doc_id})
        )
        assert original_ids

        provider.index = MagicMock(wraps=provider.index)
        provider.delete = MagicMock(wraps=provider.delete)
        documents[0].write_text(
            "# Local Store\n\n"
            "This replacement document now covers latency budgets only.\n",
            encoding="utf-8",
        )

        await lks.ingest([documents[0]])
        replacement_ids = list(
            provider.scroll_payload("bob_lks", {"source_fact_id": source_doc_id})
        )
        results = await lks.retrieve("zvec local vector store", 3)

        assert provider.delete.call_count == 1
        assert provider.index.call_count == 1
        assert replacement_ids
        assert replacement_ids != original_ids
        assert all(
            "Zvec keeps the local vector store" not in hit.payload["text"]
            for hit in results.hits
        )
    finally:
        provider.close()
        fence.close()


@pytest.mark.asyncio
async def test_restart_with_a_new_provider_child_keeps_serving_retrieval(
    workspace_path: Path,
):
    instance_root = workspace_path / "zvec"
    first_fence = _fence(workspace_path, instance_root)
    first_provider = _provider(workspace_path, instance_root, first_fence)
    documents = _write_documents(workspace_path)
    try:
        first_lks = _build_lks(instance_root, first_fence, first_provider)
        await first_lks.ingest(documents)
    finally:
        first_provider.close()
        first_fence.close()

    second_fence = _fence(workspace_path, instance_root)
    second_provider = _provider(workspace_path, instance_root, second_fence)
    try:
        second_lks = _build_lks(instance_root, second_fence, second_provider)
        results = await second_lks.retrieve("zvec local vector store", 1)

        assert results.hits
        assert "Zvec keeps the local vector store" in results.hits[0].payload["text"]
    finally:
        second_provider.close()
        second_fence.close()


@pytest.mark.asyncio
async def test_degraded_fence_refuses_ingest_with_423_shape_but_retrieves(
    workspace_path: Path,
):
    from core.lks.instance import BobLKSWriteLocked

    instance_root = workspace_path / "zvec"
    holder = _fence(workspace_path, instance_root)
    provider = _provider(workspace_path, instance_root, holder)
    contender = None
    degraded_provider = None
    try:
        lks = _build_lks(instance_root, holder, provider)
        documents = _write_documents(workspace_path)
        await lks.ingest(documents)
        provider.close()
        contender = _fence(workspace_path, instance_root)
        degraded_provider = _provider(workspace_path, instance_root, contender)
        degraded_lks = _build_lks(instance_root, contender, degraded_provider)
        degraded_provider.index = MagicMock(wraps=degraded_provider.index)
        degraded_provider.delete = MagicMock(wraps=degraded_provider.delete)

        with pytest.raises(BobLKSWriteLocked) as raised:
            await degraded_lks.ingest([documents[0]])

        assert contender.degraded is True
        assert raised.value.status_code == 423
        assert raised.value.code == "memory_write_locked"
        assert raised.value.reason == "contention"
        degraded_provider.index.assert_not_called()
        degraded_provider.delete.assert_not_called()

        results = await degraded_lks.retrieve("zvec local vector store", 1)
        assert results.hits
        assert "Zvec keeps the local vector store" in results.hits[0].payload["text"]
    finally:
        if degraded_provider is not None:
            degraded_provider.close()
        if contender is not None:
            contender.close()
        provider.close()
        holder.close()
