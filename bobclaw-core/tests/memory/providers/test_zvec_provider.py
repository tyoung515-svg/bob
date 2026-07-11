from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.ledger.federation import FederationRegistry
from core.memory.acl import ACLRegistry
from core.memory.exceptions import RetrievalProviderError
from core.memory.indexer import MemoryIndexer
from core.memory.models import ChunkRecord, ConfidenceStub, Fact, SlotResolution
from core.memory.write_fence import WriteFence, WriteFenceViolation


@pytest.fixture
def tmp_path() -> Path:
    root = Path(__file__).resolve().parents[4] / "_workspace" / "testing" / "zvec-provider-pytest"
    root.mkdir(parents=True, exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir()
    return path


def _acl(tmp_path: Path) -> ACLRegistry:
    path = tmp_path / "stores.toml"
    path.write_text(
        "[store.s]\n"
        'allowed_locality = ["local"]\n'
        'allowed_provider_ids = ["zvec-local"]\n'
        'allowed_capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    return ACLRegistry(path)


def _provider(
    tmp_path: Path,
    *,
    write_fence=None,
    collection_prefix: str = "bobclaw_",
    reclaim_timeout_s: float = 10.0,
):
    from core.memory.providers.zvec_provider import ZvecRetrievalProvider

    return ZvecRetrievalProvider(
        provider_id="zvec-local",
        locality="local",
        collection_prefix=collection_prefix,
        acl_registry=_acl(tmp_path),
        store_root=tmp_path / "zvec-root",
        write_fence=write_fence,
        reclaim_timeout_s=reclaim_timeout_s,
    )


def _fence(tmp_path: Path, collection_prefix: str = "bobclaw_") -> WriteFence:
    return WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix=collection_prefix,
        lock_dir=tmp_path / "locks",
    )


def _item(
    chunk_id: str,
    vector: list[float],
    fact_id: str = "f1",
    **payload,
) -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        vector=vector,
        payload={
            "source_fact_id": fact_id,
            "text": payload.pop("text", chunk_id),
            **payload,
        },
    )


def _fact() -> Fact:
    return Fact(
        fact_id="f1",
        generation_method="test",
        body={"text": "reindexed fact"},
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture(autouse=True)
def _close_providers():
    providers = []
    yield providers
    for provider in providers:
        provider.close()


def test_multidim_family_index_preflights_and_succeeds_under_held_fence(
    tmp_path: Path, _close_providers
):
    fence = _fence(tmp_path)
    provider = _provider(tmp_path, write_fence=fence)
    _close_providers.append(provider)
    try:
        receipt = provider.index(
            "s",
            [
                _item("chunk:f1:three", [1.0, 0.0, 0.0]),
                _item("chunk:f1:four", [0.0, 1.0, 0.0, 0.0]),
            ],
        )

        assert receipt.item_count == 2
        assert (tmp_path / "zvec-root" / "instances" / "s" / "collections" / "bobclaw__3").is_dir()
        assert (tmp_path / "zvec-root" / "instances" / "s" / "collections" / "bobclaw__4").is_dir()
        assert [hit.payload["chunk_id"] for hit in provider.query_vector("s", [1.0, 0.0, 0.0], 2).hits] == [
            "chunk:f1:three"
        ]
    finally:
        fence.close()


def test_out_of_family_fence_refuses_mutation_before_zvec_write(
    tmp_path: Path, _close_providers
):
    fence = _fence(tmp_path, collection_prefix="other_")
    provider = _provider(tmp_path, write_fence=fence)
    _close_providers.append(provider)
    try:
        with pytest.raises(WriteFenceViolation, match="outside protected family"):
            provider.index("s", [_item("chunk:f1:x", [1.0, 0.0, 0.0])])
        assert not (tmp_path / "zvec-root" / "instances" / "s").exists()
    finally:
        fence.close()


def test_delete_and_scroll_exclude_out_of_family_lookalike_directories(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index(
        "s",
        [
            _item("chunk:f1:three", [1.0, 0.0, 0.0]),
            _item("chunk:f1:four", [0.0, 1.0, 0.0, 0.0]),
        ],
    )

    collections = tmp_path / "zvec-root" / "instances" / "s" / "collections"
    for name in ("bobclaw__junk", "bobclaw___3", "other__3"):
        (collections / name).mkdir()

    chunk_ids = list(provider.scroll_payload("s", {"source_fact_id": "f1"}))
    assert set(chunk_ids) == {"chunk:f1:three", "chunk:f1:four"}
    provider.delete("s", chunk_ids)
    assert list(provider.scroll_payload("s", {"source_fact_id": "f1"})) == []


def test_reindex_and_fact_delete_cover_all_existing_family_dimensions(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index(
        "s",
        [
            _item("chunk:f1:three", [1.0, 0.0, 0.0]),
            _item("chunk:f1:four", [0.0, 1.0, 0.0, 0.0]),
        ],
    )
    fact_store = SimpleNamespace(get=AsyncMock(return_value=_fact()))
    embedder = SimpleNamespace(
        embed_doc=AsyncMock(return_value=[[0.0, 1.0, 0.0, 0.0]])
    )
    slots = SimpleNamespace(
        get=lambda _: SlotResolution(
            slot_name="embed_text",
            model="m",
            backend="b",
            endpoint="e",
            embedding_dimension=4,
        )
    )
    indexer = MemoryIndexer(
        fact_store=fact_store,
        embedder=embedder,
        provider=provider,
        store_id="s",
        slot_resolver=slots,
    )

    assert asyncio.run(indexer.drop_facts(["f1"])) == 2
    assert asyncio.run(indexer.reindex_facts(["f1"])).errors == []
    assert provider.query_vector("s", [1.0, 0.0, 0.0], 10).hits == []
    assert len(provider.query_vector("s", [0.0, 1.0, 0.0, 0.0], 10).hits) == 1


def test_none_fence_legacy_shape_still_indexes_and_uses_strict_directory_selection(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path, write_fence=None)
    _close_providers.append(provider)
    provider.index("s", [_item("chunk:f1:legacy", [1.0, 0.0, 0.0])])
    lookalike = (
        tmp_path
        / "zvec-root"
        / "instances"
        / "s"
        / "collections"
        / "bobclaw__junk"
    )
    lookalike.mkdir()

    ids = list(provider.scroll_payload("s", {"source_fact_id": "f1"}))
    provider.delete("s", ids)
    assert list(provider.scroll_payload("s", {"source_fact_id": "f1"})) == []


def test_child_crash_surfaces_clean_provider_error_then_restarted_child_serves_reads(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index("s", [_item("chunk:f1:crash", [1.0, 0.0, 0.0])])
    child = provider._child
    assert child is not None
    child.kill()
    child.wait(timeout=5)

    with pytest.raises(RetrievalProviderError, match="storage child"):
        provider.query_vector("s", [1.0, 0.0, 0.0], 1)

    recovered = provider.query_vector("s", [1.0, 0.0, 0.0], 1)
    assert [hit.payload["chunk_id"] for hit in recovered.hits] == ["chunk:f1:crash"]


def test_kill_reclaims_within_bounded_window(
    tmp_path: Path, _close_providers
):
    provider = _provider(tmp_path)
    _close_providers.append(provider)
    provider.index("s", [_item("chunk:f1:reclaim", [1.0, 0.0, 0.0])])
    child = provider._child
    assert child is not None
    child.kill()
    child.wait(timeout=5)

    with pytest.raises(RetrievalProviderError):
        provider.query_vector("s", [1.0, 0.0, 0.0], 1)

    t0 = time.monotonic()
    results = provider.query_vector("s", [1.0, 0.0, 0.0], 1)
    assert time.monotonic() - t0 <= 10.5
    assert len(results.hits) == 1


def test_two_provider_instances_on_one_store_degrade_with_honest_lock_error(
    tmp_path: Path, _close_providers
):
    first = _provider(tmp_path)
    second = _provider(tmp_path, reclaim_timeout_s=0.5)
    _close_providers.extend([first, second])
    first.index("s", [_item("chunk:f1:lock", [1.0, 0.0, 0.0])])

    with pytest.raises(RetrievalProviderError, match="reclaim timed out.*Can't lock"):
        second.query_vector("s", [1.0, 0.0, 0.0], 1)

    health = second.health()
    assert health.ok is False
    assert "Can't lock" in health.detail



def test_provider_conforms_to_declared_retrieval_protocol(
    tmp_path: Path, _close_providers
):
    from core.memory.interfaces import RetrievalProvider

    provider = _provider(tmp_path)
    _close_providers.append(provider)
    assert isinstance(provider, RetrievalProvider)
