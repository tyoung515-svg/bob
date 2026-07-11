from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectorError

from core.memory.embedder import SlotResolvedEmbedder
from core.memory.exceptions import EmbedderUnavailable, SlotMisconfigured
from core.memory.models import SlotResolution


def _mock_resolver(
    backend: str = "lmstudio",
    endpoint: str = "http://localhost:1234",
    embedding_dimension: int | None = 768,
) -> MagicMock:
    resolver = MagicMock()
    resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-embedder-model",
        backend=backend,
        endpoint=endpoint,
        embedding_dimension=embedding_dimension,
    )
    return resolver


def _make_embedder(
    backend: str = "lmstudio",
    endpoint: str = "http://localhost:1234",
    embedding_dimension: int | None = 768,
) -> SlotResolvedEmbedder:
    resolver = _mock_resolver(backend, endpoint, embedding_dimension)
    return SlotResolvedEmbedder(resolver, "embed_text")


def _mock_aiohttp_post(status: int = 200, body: dict | None = None) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status = status
    async def _json(*args, **kwargs):
        return body or {}
    mock_resp.json = _json
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


# ─── test_init_resolves_slot ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_init_resolves_slot():
    resolver = _mock_resolver()
    embedder = SlotResolvedEmbedder(resolver, "embed_text")
    resolver.get.assert_called_once_with("embed_text")
    assert embedder.embedding_dimension == 768
    assert embedder._backend == "lmstudio"
    assert embedder._model == "test-embedder-model"


# ─── test_embed_lmstudio_calls_v1_embeddings ─────────────────────────────────

@pytest.mark.asyncio
async def test_embed_lmstudio_calls_v1_embeddings():
    # dim=3 matches the 3-element fixture (the MS2-C1 guard checks vec length == embedding_dimension)
    embedder = _make_embedder(embedding_dimension=3)
    lmstudio_body = {"data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}]}
    mock_cm = _mock_aiohttp_post(200, lmstudio_body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm) as mock_post:
        result = await embedder.embed(["hello world"])

    assert result == [[0.1, 0.2, 0.3]]
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:1234/v1/embeddings"
    assert kwargs["json"]["model"] == "test-embedder-model"
    assert kwargs["json"]["input"] == ["hello world"]


# ─── test_embed_ollama_calls_api_embeddings ───────────────────────────────────

@pytest.mark.asyncio
async def test_embed_ollama_calls_api_embeddings():
    # dim=3 matches the 3-element fixture (the MS2-C1 guard checks vec length == embedding_dimension)
    embedder = _make_embedder(backend="ollama", embedding_dimension=3)
    ollama_body = {"embedding": [0.4, 0.5, 0.6]}
    mock_cm = _mock_aiohttp_post(200, ollama_body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm) as mock_post:
        result = await embedder.embed(["hello"])

    assert result == [[0.4, 0.5, 0.6]]
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:1234/api/embeddings"
    assert kwargs["json"]["model"] == "test-embedder-model"
    assert kwargs["json"]["prompt"] == "hello"


# ─── test_embed_unsupported_backend_raises ────────────────────────────────────

def test_embed_unsupported_backend_raises():
    resolver = _mock_resolver(backend="bogus")
    with pytest.raises(SlotMisconfigured) as excinfo:
        SlotResolvedEmbedder(resolver, "embed_text")
    assert "bogus" in str(excinfo.value)


# ─── test_embed_unreachable_raises_embedder_unavailable ───────────────────────

@pytest.mark.asyncio
async def test_embed_unreachable_raises_embedder_unavailable():
    embedder = _make_embedder()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(
        side_effect=ClientConnectorError(MagicMock(), MagicMock())
    )
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(EmbedderUnavailable) as excinfo:
            await embedder.embed(["hello"])

    assert "http://localhost:1234" in str(excinfo.value)


# ─── test_embed_malformed_response_raises_embedder_unavailable ────────────────

@pytest.mark.asyncio
async def test_embed_malformed_response_raises_embedder_unavailable():
    embedder = _make_embedder()
    malformed_body = {"unexpected": "shape"}
    mock_cm = _mock_aiohttp_post(200, malformed_body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(EmbedderUnavailable):
            await embedder.embed(["hello"])


# ─── test_embed_missing_embedding_dimension_in_slot_raises ────────────────────

def test_embed_missing_embedding_dimension_in_slot_raises():
    resolver = _mock_resolver(embedding_dimension=None)
    with pytest.raises(SlotMisconfigured) as excinfo:
        SlotResolvedEmbedder(resolver, "embed_text")
    assert "embedding_dimension" in str(excinfo.value)


# ─── test_returns_correct_dimensionality ──────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_correct_dimensionality():
    embedder = _make_embedder(embedding_dimension=768)
    # non-zero healthy vector: the MS2-C1 zero-vector guard rejects all-zero vectors for
    # non-empty text, so this dimensionality fixture uses a healthy vector.
    dummy_vec = [0.1] * 768
    lmstudio_body = {"data": [
        {"embedding": dummy_vec, "index": 0},
        {"embedding": dummy_vec, "index": 1},
    ]}
    mock_cm = _mock_aiohttp_post(200, lmstudio_body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        result = await embedder.embed(["x", "y"])

    assert len(result) == 2
    assert all(len(v) == 768 for v in result)
