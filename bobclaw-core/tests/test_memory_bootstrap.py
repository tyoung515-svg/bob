from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

import core.memory.bootstrap as bootstrap_mod
from core.memory.bootstrap import (
    MemoryBootstrapConfig,
    bootstrap_memory,
    get_memory,
)
from core.memory.exceptions import MemoryConfigError


@pytest.fixture(autouse=True)
def _reset_bootstrap_globals() -> None:
    bootstrap_mod._bootstrap_singleton = None
    bootstrap_mod._bootstrap_config_snapshot = None


@pytest.fixture
def stores_toml(tmp_path: Path) -> Path:
    path = tmp_path / "test_stores.toml"
    path.write_text(
        "[stores.test_store]\n"
        'acl_allowed_providers = ["test_provider"]\n'
        "\n"
        "[providers.test_provider]\n"
        'locality = "local"\n'
        'collection_prefix = "test_"\n'
        'capability_classes = ["text_dense"]\n'
    )
    return path


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    return tmp_path / "test_memory.db"


@pytest.fixture
def mock_qdrant_client() -> MagicMock:
    client = MagicMock()
    client.get_collections.return_value = MagicMock()
    return client


def _make_config(
    sqlite_path: Path,
    stores_path: Path,
    **kwargs,
) -> MemoryBootstrapConfig:
    return MemoryBootstrapConfig(
        enabled=True,
        sqlite_path=sqlite_path,
        qdrant_url=kwargs.pop("qdrant_url", "http://localhost:16333"),
        stores_config_path=stores_path,
        default_store_id=kwargs.pop("default_store_id", "test_store"),
    )


class TestBootstrapIdempotent:
    @patch("core.memory.bootstrap.QdrantClient")
    def test_bootstrap_idempotent(
        self,
        mock_qdrant_cls: MagicMock,
        stores_toml: Path,
        sqlite_path: Path,
    ) -> None:
        mock_qdrant_cls.return_value = MagicMock()
        mock_qdrant_cls.return_value.get_collections.return_value = (
            MagicMock()
        )
        config = _make_config(sqlite_path, stores_toml)
        s1 = bootstrap_memory(config)
        s2 = bootstrap_memory(config)
        assert s1 is s2


class TestBootstrapRejectsDifferentConfig:
    @patch("core.memory.bootstrap.QdrantClient")
    def test_bootstrap_rejects_different_config(
        self,
        mock_qdrant_cls: MagicMock,
        stores_toml: Path,
        sqlite_path: Path,
        tmp_path: Path,
    ) -> None:
        mock_qdrant_cls.return_value = MagicMock()
        mock_qdrant_cls.return_value.get_collections.return_value = (
            MagicMock()
        )
        config_a = _make_config(sqlite_path, stores_toml)
        bootstrap_memory(config_a)
        other_db = tmp_path / "other.db"
        config_b = _make_config(other_db, stores_toml)
        with pytest.raises(MemoryConfigError, match="bootstrap already called with different config"):
            bootstrap_memory(config_b)


class TestBootstrapQdrantUnreachable:
    @patch("core.memory.bootstrap.QdrantClient")
    def test_bootstrap_qdrant_unreachable_raises(
        self,
        mock_qdrant_cls: MagicMock,
        stores_toml: Path,
        sqlite_path: Path,
    ) -> None:
        mock_qdrant_cls.side_effect = ConnectionError("refused")
        config = _make_config(sqlite_path, stores_toml)
        with pytest.raises(
            MemoryConfigError,
            match="Qdrant unreachable at http://localhost:16333 after 10s",
        ):
            bootstrap_memory(config)


class TestGetMemoryBeforeBootstrap:
    def test_get_memory_before_bootstrap_raises(self) -> None:
        bootstrap_mod._bootstrap_singleton = None
        with pytest.raises(
            MemoryConfigError, match="memory not bootstrapped"
        ):
            get_memory()


class TestBootstrapCreatesSchema:
    @patch("core.memory.bootstrap.QdrantClient")
    def test_bootstrap_creates_sqlite_schema_if_missing(
        self,
        mock_qdrant_cls: MagicMock,
        stores_toml: Path,
        sqlite_path: Path,
    ) -> None:
        mock_qdrant_cls.return_value = MagicMock()
        mock_qdrant_cls.return_value.get_collections.return_value = (
            MagicMock()
        )
        assert not sqlite_path.exists()
        config = _make_config(sqlite_path, stores_toml)
        bootstrap_memory(config)
        assert sqlite_path.exists()

        async def _check_tables() -> list[str]:
            async with aiosqlite.connect(str(sqlite_path)) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

        tables = asyncio.run(_check_tables())
        assert "memory_events" in tables
        assert "memory_facts" in tables
