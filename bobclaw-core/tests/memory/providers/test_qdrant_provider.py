from __future__ import annotations

import ast
import inspect
from pathlib import Path
import textwrap
from unittest.mock import MagicMock, Mock

import pytest

from core.memory.acl import ACLRegistry
from core.memory.exceptions import ACLViolation, RetrievalProviderError
from core.memory.models import (
    ChunkRecord,
    HealthStatus,
    Hit,
    IndexReceipt,
    RankedResults,
)


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def permissive_acl(tmp_path) -> ACLRegistry:
    f = tmp_path / "stores.toml"
    f.write_text(
        """
[store.test_store]
allowed_locality = ["local"]
allowed_provider_ids = ["qdrant-local"]
allowed_capability_classes = ["text_dense"]

[store.restricted]
allowed_locality = ["remote"]
allowed_provider_ids = ["other-provider"]
allowed_capability_classes = ["text_sparse"]
""",
        encoding="utf-8",
    )
    return ACLRegistry(f)


@pytest.fixture
def provider(permissive_acl, mock_client):
    from core.memory.providers.qdrant_provider import (
        QdrantRetrievalProvider,
    )

    return QdrantRetrievalProvider(
        provider_id="qdrant-local",
        locality="local",
        collection_prefix="bobclaw_l1_text_dense",
        acl_registry=permissive_acl,
        client=mock_client,
    )


def _protocol_methods(protocol: type) -> set[str]:
    return {
        name
        for name, member in vars(protocol).items()
        if not name.startswith("_") and callable(member)
    }


def _is_unconditionally_unimplemented(method) -> bool:
    source = textwrap.dedent(inspect.getsource(method))
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)

    return len(body) == 1 and (
        isinstance(body[0], (ast.Pass, ast.Raise))
        or (
            isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and body[0].value.value is Ellipsis
        )
    )


class TestConstructor:
    def test_constructor_succeeds(self, provider):
        assert provider.provider_id == "qdrant-local"
        assert provider.locality == "local"
        assert provider.collection_prefix == "bobclaw_l1_text_dense"

    def test_capability_classes(self, provider):
        assert provider.capability_classes == {"text_dense"}


class TestIndex:
    def test_index_upserts_with_correct_collection_per_dim(
        self, provider, mock_client, permissive_acl
    ):
        mock_client.get_collection.return_value = Mock()

        items = [
            ChunkRecord(
                id="c1",
                vector=[0.1, 0.2, 0.3],
                payload={"text": "hello"},
            ),
            ChunkRecord(
                id="c2",
                vector=[0.4, 0.5, 0.6],
                payload={"text": "world"},
            ),
        ]
        receipt = provider.index("test_store", items)

        assert receipt.item_count == 2
        assert receipt.provider_id == "qdrant-local"
        assert receipt.store_id == "test_store"
        mock_client.upsert.assert_called_once()
        call_kwargs = mock_client.upsert.call_args[1]
        assert call_kwargs["collection_name"] == "bobclaw_l1_text_dense_3"

    def test_index_auto_creates_collection(
        self, provider, mock_client, permissive_acl
    ):
        mock_client.get_collection.side_effect = Exception("not found")

        items = [
            ChunkRecord(
                id="c1",
                vector=[0.1, 0.2],
                payload={"text": "hi"},
            )
        ]
        provider.index("test_store", items)

        mock_client.create_collection.assert_called_once()
        call_kwargs = mock_client.create_collection.call_args[1]
        assert call_kwargs["collection_name"] == "bobclaw_l1_text_dense_2"

    def test_index_returns_receipt_with_item_count(
        self, provider, mock_client, permissive_acl
    ):
        mock_client.get_collection.return_value = Mock()

        items = [
            ChunkRecord(
                id="c1",
                vector=[0.1, 0.2, 0.3],
                payload={"text": "a"},
            ),
            ChunkRecord(
                id="c2",
                vector=[0.4, 0.5, 0.6],
                payload={"text": "b"},
            ),
            ChunkRecord(
                id="c3",
                vector=[0.7, 0.8, 0.9],
                payload={"text": "c"},
            ),
        ]
        receipt = provider.index("test_store", items)
        assert isinstance(receipt, IndexReceipt)
        assert receipt.item_count == 3

    def test_index_raises_acl_violation_when_disallowed(
        self, mock_client, permissive_acl
    ):
        from core.memory.providers.qdrant_provider import (
            QdrantRetrievalProvider,
        )

        prov = QdrantRetrievalProvider(
            provider_id="bad-actor",
            locality="remote",
            collection_prefix="bobclaw_l1_text_dense",
            acl_registry=permissive_acl,
            client=mock_client,
        )
        items = [
            ChunkRecord(
                id="c1",
                vector=[0.1, 0.2],
                payload={"text": "x"},
            )
        ]
        with pytest.raises(ACLViolation):
            prov.index("test_store", items)


class TestQueryVector:
    def test_query_vector_calls_query_points(
        self, provider, mock_client, permissive_acl
    ):
        from types import SimpleNamespace

        from qdrant_client.http.models import ScoredPoint

        mock_client.query_points.return_value = SimpleNamespace(
            points=[
                ScoredPoint(
                    id="hit1",
                    version=0,
                    score=0.95,
                    payload={"text": "result"},
                    vector=None,
                ),
                ScoredPoint(
                    id="hit2",
                    version=0,
                    score=0.80,
                    payload={"text": "other"},
                    vector=None,
                ),
            ]
        )

        vector = [0.1, 0.2, 0.3]
        results = provider.query_vector(
            "test_store", vector, k=5, filters=None
        )

        assert isinstance(results, RankedResults)
        assert len(results.hits) == 2
        assert results.hits[0].score >= results.hits[1].score
        mock_client.query_points.assert_called_once()
        call_kwargs = mock_client.query_points.call_args[1]
        assert call_kwargs["collection_name"] == "bobclaw_l1_text_dense_3"
        assert call_kwargs["query"] == vector
        assert call_kwargs["limit"] == 5

    def test_query_vector_returns_sorted_hits(
        self, provider, mock_client, permissive_acl
    ):
        from types import SimpleNamespace

        from qdrant_client.http.models import ScoredPoint

        mock_client.query_points.return_value = SimpleNamespace(
            points=[
                ScoredPoint(
                    id="a",
                    version=0,
                    score=0.50,
                    payload={},
                    vector=None,
                ),
                ScoredPoint(
                    id="b",
                    version=0,
                    score=0.95,
                    payload={},
                    vector=None,
                ),
                ScoredPoint(
                    id="c",
                    version=0,
                    score=0.75,
                    payload={},
                    vector=None,
                ),
            ]
        )

        results = provider.query_vector("test_store", [0.1, 0.2, 0.3], k=3)
        scores = [h.score for h in results.hits]
        assert scores == [0.95, 0.75, 0.50]

    def test_query_vector_raises_acl_violation_when_disallowed(
        self, mock_client, permissive_acl
    ):
        from core.memory.providers.qdrant_provider import (
            QdrantRetrievalProvider,
        )

        prov = QdrantRetrievalProvider(
            provider_id="bad-actor",
            locality="remote",
            collection_prefix="bobclaw_l1_text_dense",
            acl_registry=permissive_acl,
            client=mock_client,
        )
        with pytest.raises(ACLViolation):
            prov.query_vector("test_store", [0.1, 0.2], k=1)


class TestQueryProtocol:
    def test_scroll_payload_calls_scroll_with_correct_filter(
        self, provider, mock_client, permissive_acl
    ):
        c = Mock()
        c.name = "bobclaw_l1_text_dense_3"
        mock_client.get_collections.return_value = Mock(collections=[c])
        mock_client.scroll.return_value = ([], None)

        ids = list(provider.scroll_payload("test_store", {"source_fact_id": "f1"}))
        assert ids == []
        mock_client.scroll.assert_called_once()
        call_kwargs = mock_client.scroll.call_args[1]
        assert call_kwargs["collection_name"] == "bobclaw_l1_text_dense_3"
        assert call_kwargs["limit"] == 128
        assert call_kwargs["with_payload"] is False
        assert call_kwargs["with_vectors"] is False

    def test_scroll_payload_iterates_pages(
        self, provider, mock_client, permissive_acl
    ):
        c = Mock()
        c.name = "bobclaw_l1_text_dense_3"
        mock_client.get_collections.return_value = Mock(collections=[c])

        p1 = Mock(); p1.id = "id1"
        p2 = Mock(); p2.id = "id2"
        page1 = ([p1, p2], "next")
        page2 = ([], None)
        mock_client.scroll.side_effect = [page1, page2]

        ids = list(provider.scroll_payload("test_store", {"source_fact_id": "f1"}))
        assert ids == ["id1", "id2"]
        assert mock_client.scroll.call_count == 2

    def test_scroll_payload_raises_acl_violation(
        self, mock_client, permissive_acl
    ):
        from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
        prov = QdrantRetrievalProvider(
            provider_id="bad-actor",
            locality="remote",
            collection_prefix="bobclaw_l1_text_dense",
            acl_registry=permissive_acl,
            client=mock_client,
        )
        with pytest.raises(ACLViolation):
            list(prov.scroll_payload("test_store", {"source_fact_id": "f1"}))

    def test_scroll_payload_raises_on_client_failure(
        self, provider, mock_client, permissive_acl
    ):
        c = Mock()
        c.name = "bobclaw_l1_text_dense_3"
        mock_client.get_collections.return_value = Mock(collections=[c])
        mock_client.scroll.side_effect = Exception("connection refused")

        with pytest.raises(RetrievalProviderError):
            list(provider.scroll_payload("test_store", {"source_fact_id": "f1"}))

    def test_scroll_payload_returns_empty_when_no_match(
        self, provider, mock_client, permissive_acl
    ):
        c = Mock()
        c.name = "bobclaw_l1_text_dense_3"
        mock_client.get_collections.return_value = Mock(collections=[c])
        mock_client.scroll.return_value = ([], None)

        ids = list(provider.scroll_payload("test_store", {"source_fact_id": "nonexistent"}))
        assert ids == []


class TestDelete:
    def test_delete_calls_client_delete(
        self, provider, mock_client, permissive_acl
    ):
        c1, c2, c3 = Mock(), Mock(), Mock()
        c1.name = "bobclaw_l1_text_dense_3"
        c2.name = "bobclaw_l1_text_dense_768"
        c3.name = "other_collection"
        mock_client.get_collections.return_value = Mock(
            collections=[c1, c2, c3]
        )

        provider.delete("test_store", ["id1", "id2"])

        assert mock_client.delete.call_count == 2
        call_collections = [
            c[1]["collection_name"]
            for c in mock_client.delete.call_args_list
        ]
        assert "bobclaw_l1_text_dense_3" in call_collections
        assert "bobclaw_l1_text_dense_768" in call_collections
        assert "other_collection" not in call_collections


class TestHealth:
    def test_health_ok_when_client_succeeds(
        self, provider, mock_client, permissive_acl
    ):
        mock_client.get_collections.return_value = Mock()
        status = provider.health()
        assert isinstance(status, HealthStatus)
        assert status.ok is True
        assert status.detail == ""

    def test_health_not_ok_on_exception(
        self, provider, mock_client, permissive_acl
    ):
        mock_client.get_collections.side_effect = Exception("connection refused")
        status = provider.health()
        assert status.ok is False
        assert "connection refused" in status.detail


class TestProtocolConformance:
    def test_isinstance_retrieval_provider(self, provider):
        from core.memory.interfaces import RetrievalProvider

        assert isinstance(provider, RetrievalProvider)

    def test_declared_methods_are_implemented_and_live(self):
        from core.memory.interfaces import RetrievalProvider
        from core.memory.providers.qdrant_provider import QdrantRetrievalProvider

        declared_methods = _protocol_methods(RetrievalProvider)
        missing_methods = declared_methods - set(vars(QdrantRetrievalProvider))
        assert not missing_methods

        dead_methods = [
            name
            for name in declared_methods
            if _is_unconditionally_unimplemented(
                vars(QdrantRetrievalProvider)[name]
            )
        ]
        assert not dead_methods
