from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.embedder import SlotResolvedEmbedder
from core.memory.exceptions import EmbedderUnavailable
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


# ─── Case 1: zero vector raises ──────────────────────────────────────────

async def test_zero_vector_raises():
    embedder = _make_embedder()
    zero_vec = [0.0] * 768
    body = {"data": [{"embedding": zero_vec, "index": 0}]}
    mock_cm = _mock_aiohttp_post(200, body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(EmbedderUnavailable) as excinfo:
            await embedder.embed(["real non-empty text"])

    assert "http://localhost:1234" in str(excinfo.value)


# ─── Case 2: degenerate tiny vector raises ────────────────────────────────

async def test_degenerate_tiny_vector_raises():
    embedder = _make_embedder()
    tiny_vec = [1e-12] * 768
    body = {"data": [{"embedding": tiny_vec, "index": 0}]}
    mock_cm = _mock_aiohttp_post(200, body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(EmbedderUnavailable):
            await embedder.embed(["real non-empty text"])


# ─── Case 3: dimension mismatch raises ────────────────────────────────────

async def test_dim_mismatch_raises():
    embedder = _make_embedder()
    wrong_dim_vec = [0.1] * 512
    body = {"data": [{"embedding": wrong_dim_vec, "index": 0}]}
    mock_cm = _mock_aiohttp_post(200, body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(EmbedderUnavailable) as excinfo:
            await embedder.embed(["real non-empty text"])

    msg = str(excinfo.value)
    assert "512" in msg
    assert "768" in msg


# ─── Case 4: count mismatch raises (direct _guard_batch) ──────────────────

def test_count_mismatch_raises():
    embedder = _make_embedder()
    with pytest.raises(EmbedderUnavailable) as excinfo:
        embedder._guard_batch(["a", "b"], [[0.1] * 768])

    assert "returned 1 vectors for 2 inputs" in str(excinfo.value)


# ─── Case 5: healthy vector passes unchanged ──────────────────────────────

async def test_healthy_vector_passes_unchanged():
    embedder = _make_embedder()
    healthy_vec = [0.1] * 768
    body = {"data": [{"embedding": healthy_vec, "index": 0}]}
    mock_cm = _mock_aiohttp_post(200, body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        result = await embedder.embed(["hello"])

    assert result == [[0.1] * 768]


# ─── Case 6: single non-zero element passes boundary ──────────────────────

async def test_single_nonzero_element_passes_boundary():
    embedder = _make_embedder()

    # First: pass (single non-zero element above 1e-9)
    passing_vec = [0.0] * 767 + [0.1]
    body_pass = {"data": [{"embedding": passing_vec, "index": 0}]}
    mock_cm_pass = _mock_aiohttp_post(200, body_pass)
    with patch("aiohttp.ClientSession.post", return_value=mock_cm_pass):
        result = await embedder.embed(["x"])
    assert result == [passing_vec]

    # Second: fail (all zero)
    failing_vec = [0.0] * 768
    body_fail = {"data": [{"embedding": failing_vec, "index": 0}]}
    mock_cm_fail = _mock_aiohttp_post(200, body_fail)
    with patch("aiohttp.ClientSession.post", return_value=mock_cm_fail):
        with pytest.raises(EmbedderUnavailable):
            await embedder.embed(["x"])


# ─── Case 7: empty text preserved zero ok ─────────────────────────────────

async def test_empty_text_preserved_zero_ok():
    embedder = _make_embedder()
    zero_vec = [0.0] * 768
    body = {"data": [{"embedding": zero_vec, "index": 0}]}
    mock_cm = _mock_aiohttp_post(200, body)

    # empty string
    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        result = await embedder.embed([""])
    assert result == [zero_vec]

    # whitespace string
    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        result = await embedder.embed(["   "])
    assert result == [zero_vec]


# ─── Case 8: ollama backend zero vector raises ────────────────────────────

async def test_ollama_zero_vector_raises():
    embedder = _make_embedder(backend="ollama")
    zero_vec = [0.0] * 768
    body = {"embedding": zero_vec}
    mock_cm = _mock_aiohttp_post(200, body)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(EmbedderUnavailable) as excinfo:
            await embedder.embed(["real"])

    assert "http://localhost:1234" in str(excinfo.value)


# ─── Case 9: mixed batch — empty skipped, poisoned non-empty raises ───────

def test_mixed_batch_empty_skipped_but_poisoned_nonempty_raises():
    embedder = _make_embedder()

    # Good + empty -> empty skipped, passes (no raise)
    embedder._guard_batch(["good", ""], [[0.1] * 768, [0.0] * 768])

    # Good + bad (poisoned non-empty) -> raises
    with pytest.raises(EmbedderUnavailable):
        embedder._guard_batch(["good", "bad"], [[0.1] * 768, [0.0] * 768])
