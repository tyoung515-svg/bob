"""
BoBClaw Core — Unit tests for LocalModelRouter

All HTTP calls are intercepted with aioresponses; no real backends required.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses

from core.backends.local_router import LocalBackendInfo, LocalModelRouter

# ─── Fixture payloads ─────────────────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434"
LMSTUDIO_URL = "http://localhost:1234"

OLLAMA_MODELS = {
    "data": [
        {"id": "gemma-4-27b"},
        {"id": "llama3:8b"},
        {"id": "mistral:7b"},
    ]
}

LMSTUDIO_MODELS = {
    "data": [
        {"id": "lmstudio-community/gemma-4-26b-GGUF"},
        {"id": "phi-4"},
    ]
}

REASONING_MODELS = {
    "data": [
        {"id": "gemma-4-27b"},
        {"id": "deepseek-r1:70b"},
    ]
}


# ─── discover() ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_both_available():
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models",   payload=OLLAMA_MODELS)
        m.get(f"{LMSTUDIO_URL}/v1/models", payload=LMSTUDIO_MODELS)
        backends = await router.discover()

    assert len(backends) == 2
    names = {b.name for b in backends}
    assert names == {"ollama", "lmstudio"}
    ollama = next(b for b in backends if b.name == "ollama")
    assert "gemma-4-27b" in ollama.models
    lms = next(b for b in backends if b.name == "lmstudio")
    assert "lmstudio-community/gemma-4-26b-GGUF" in lms.models


@pytest.mark.asyncio
async def test_discover_only_ollama():
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", payload=OLLAMA_MODELS)
        # LM Studio not registered → aioresponses raises → caught → []
        backends = await router.discover()

    assert len(backends) == 1
    assert backends[0].name == "ollama"
    assert router._cached_backends == backends


@pytest.mark.asyncio
async def test_discover_only_lmstudio():
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{LMSTUDIO_URL}/v1/models", payload=LMSTUDIO_MODELS)
        # Ollama not registered → caught → []
        backends = await router.discover()

    assert len(backends) == 1
    assert backends[0].name == "lmstudio"


@pytest.mark.asyncio
async def test_discover_neither_available():
    router = LocalModelRouter()
    with aioresponses():
        # Nothing registered → both fail
        backends = await router.discover()

    assert backends == []
    assert router._cached_backends == []


@pytest.mark.asyncio
async def test_discover_bad_status_excluded():
    """A 500 from a backend should produce an empty model list → backend excluded."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models",   status=500)
        m.get(f"{LMSTUDIO_URL}/v1/models", payload=LMSTUDIO_MODELS)
        backends = await router.discover()

    assert len(backends) == 1
    assert backends[0].name == "lmstudio"


# ─── _pick_model() ────────────────────────────────────────────────────────────

def test_pick_model_selects_gemma_27b():
    models = ["llama3:8b", "mistral:7b", "gemma-4-27b", "phi-4"]
    assert LocalModelRouter._pick_model(models) == "gemma-4-27b"


def test_pick_model_selects_gemma_26b():
    models = ["llama3:8b", "gemma-4-26b", "phi-4"]
    assert LocalModelRouter._pick_model(models) == "gemma-4-26b"


def test_pick_model_27b_beats_26b():
    models = ["gemma-4-26b", "gemma-4-27b", "phi-4"]
    assert LocalModelRouter._pick_model(models) == "gemma-4-26b"  # first hit wins (26b matches 2[76]b)


def test_pick_model_falls_back_to_any_gemma():
    models = ["llama3:8b", "gemma:2b", "phi-4"]
    assert LocalModelRouter._pick_model(models) == "gemma:2b"


def test_pick_model_falls_back_to_first_model():
    models = ["llama3:8b", "phi-4"]
    assert LocalModelRouter._pick_model(models) == "llama3:8b"


def test_pick_model_empty_list_returns_none():
    assert LocalModelRouter._pick_model([]) is None


def test_pick_model_case_insensitive_gemma_27b():
    models = ["Gemma-4-27B", "llama3:8b"]
    assert LocalModelRouter._pick_model(models) == "Gemma-4-27B"


def test_pick_model_lmstudio_gguf_path():
    models = ["lmstudio-community/gemma-4-26b-GGUF", "phi-4"]
    result = LocalModelRouter._pick_model(models)
    assert result == "lmstudio-community/gemma-4-26b-GGUF"


# ─── _pick_model(): resident preference (Sprint 04 hardening) ────────────────

def test_pick_model_resident_preferred_when_resident_provided():
    """When residency info is available, gemma-26b in the *resident* set wins
    over a higher-preference gemma that's installed but not resident."""
    available = ["gemma-4-27b", "gemma-4-26b", "phi-4"]
    resident = ["gemma-4-26b", "phi-4"]
    # Static order on available would prefer gemma-4-27b, but it's not
    # resident. The router should pick the resident gemma-4-26b instead.
    assert LocalModelRouter._pick_model(available, resident=resident) == "gemma-4-26b"


def test_pick_model_resident_first_even_when_no_gemma_resident():
    """Resident list with no gemma → first resident, not the highest-pref
    gemma from the installed list."""
    available = ["gemma-4-27b", "llama3:8b", "phi-4"]
    resident = ["llama3:8b"]
    assert LocalModelRouter._pick_model(available, resident=resident) == "llama3:8b"


def test_pick_model_empty_resident_falls_back_to_legacy_order():
    """An empty resident list is treated as 'no residency info' — legacy
    static order on the installed list is preserved."""
    available = ["llama3:8b", "gemma-4-27b", "phi-4"]
    assert LocalModelRouter._pick_model(available, resident=[]) == "gemma-4-27b"


def test_pick_model_none_resident_falls_back_to_legacy_order():
    """Explicit None resident → legacy order (the pre-hardening behavior)."""
    available = ["llama3:8b", "gemma-4-27b", "phi-4"]
    assert LocalModelRouter._pick_model(available, resident=None) == "gemma-4-27b"


def test_pick_model_resident_gemma_27b_beats_resident_anything():
    """Resident ranking still follows the static gemma-27b/26b preference
    within the resident set."""
    available = ["gemma-4-26b", "gemma-4-27b", "llama3:8b"]
    resident = ["llama3:8b", "gemma-4-27b"]
    assert LocalModelRouter._pick_model(available, resident=resident) == "gemma-4-27b"


# ─── get_best_backend() ───────────────────────────────────────────────────────

def _make_backends():
    ollama   = LocalBackendInfo("ollama",   OLLAMA_URL,   ["gemma-4-27b"])
    lmstudio = LocalBackendInfo("lmstudio", LMSTUDIO_URL, ["lmstudio-community/gemma-4-26b-GGUF"])
    return ollama, lmstudio


def test_get_best_backend_linux_prefers_ollama():
    ollama, lmstudio = _make_backends()
    router = LocalModelRouter()
    with patch("core.backends.local_router.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = router.get_best_backend([ollama, lmstudio])
    assert result == ollama


def test_get_best_backend_windows_prefers_lmstudio():
    ollama, lmstudio = _make_backends()
    router = LocalModelRouter()
    with patch("core.backends.local_router.sys") as mock_sys:
        mock_sys.platform = "win32"
        result = router.get_best_backend([ollama, lmstudio])
    assert result == lmstudio


def test_get_best_backend_linux_falls_back_to_lmstudio():
    """Ollama absent on Linux → fall back to LM Studio."""
    _, lmstudio = _make_backends()
    router = LocalModelRouter()
    with patch("core.backends.local_router.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = router.get_best_backend([lmstudio])
    assert result == lmstudio


def test_get_best_backend_windows_falls_back_to_ollama():
    """LM Studio absent on Windows → fall back to Ollama."""
    ollama, _ = _make_backends()
    router = LocalModelRouter()
    with patch("core.backends.local_router.sys") as mock_sys:
        mock_sys.platform = "win32"
        result = router.get_best_backend([ollama])
    assert result == ollama


def test_get_best_backend_empty_returns_none():
    router = LocalModelRouter()
    assert router.get_best_backend([]) is None


def test_get_best_backend_uses_cache_when_no_arg():
    ollama, _ = _make_backends()
    router = LocalModelRouter()
    router._cached_backends = [ollama]
    with patch("core.backends.local_router.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = router.get_best_backend()
    assert result == ollama


# ─── get_reasoning_model() ────────────────────────────────────────────────────

def test_get_reasoning_model_finds_70b():
    backends = [
        LocalBackendInfo("ollama", OLLAMA_URL, ["gemma-4-27b", "deepseek-r1:70b"]),
    ]
    router = LocalModelRouter()
    result = router.get_reasoning_model(backends)
    assert result is not None
    backend, model = result
    assert model == "deepseek-r1:70b"


def test_get_reasoning_model_returns_none_when_only_small():
    backends = [
        LocalBackendInfo("ollama", OLLAMA_URL, ["gemma-4-27b", "llama3:8b"]),
    ]
    router = LocalModelRouter()
    assert router.get_reasoning_model(backends) is None


def test_get_reasoning_model_exact_31b_qualifies():
    backends = [
        LocalBackendInfo("ollama", OLLAMA_URL, ["some-model-31b"]),
    ]
    router = LocalModelRouter()
    result = router.get_reasoning_model(backends)
    assert result is not None
    _, model = result
    assert model == "some-model-31b"


# ─── discover_embedding_models() ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_embedding_models_filters():
    """Only models with embedding-related keywords are returned."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models",   payload={"data": [
            {"id": "gemma-4-27b"},
            {"id": "granite-embedding-311m"},
            {"id": "nomic-embed-text"},
            {"id": "llama3:8b"},
        ]})
        m.get(f"{LMSTUDIO_URL}/v1/models", payload={"data": [
            {"id": "bge-m3"},
            {"id": "qwen3-embedding-4b"},
            {"id": "phi-4"},
        ]})
        await router.discover()

    results = await router.discover_embedding_models()
    names = [m for _, m in results]
    assert "granite-embedding-311m" in names
    assert "nomic-embed-text" in names
    assert "bge-m3" in names
    assert "qwen3-embedding-4b" in names
    assert "gemma-4-27b" not in names
    assert "llama3:8b" not in names
    assert "phi-4" not in names


# ─── embed() ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_embed_returns_sorted_vectors():
    """Embeddings are sorted by index even when API returns out of order."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", payload={"data": [{"id": "granite-embedding-311m"}]})
        await router.discover()

        m.post(f"{OLLAMA_URL}/v1/embeddings", payload={
            "data": [
                {"index": 1, "embedding": [0.2, 0.3]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ],
            "model": "granite-embedding-311m",
        })

        result = await router.embed(
            texts=["hello", "world"],
            model="granite-embedding-311m",
        )

    assert len(result) == 2
    assert result[0] == [0.1, 0.2]
    assert result[1] == [0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_unknown_model_raises():
    """Model not present on any backend raises RuntimeError."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", payload={"data": [{"id": "gemma-4-27b"}]})
        await router.discover()

    with pytest.raises(RuntimeError, match="not on any discovered backend"):
        await router.embed(texts=["test"], model="nonexistent-model")


@pytest.mark.asyncio
async def test_embed_propagates_500():
    """Server error raises, not swallowed."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", payload={"data": [{"id": "granite-embedding-311m"}]})
        await router.discover()

        m.post(f"{OLLAMA_URL}/v1/embeddings", status=500)

        with pytest.raises(Exception):
            await router.embed(
                texts=["test"],
                model="granite-embedding-311m",
            )


# ─── discover() populates resident_models (Sprint 04 hardening) ──────────────

@pytest.mark.asyncio
async def test_discover_populates_resident_ollama_from_api_ps():
    """Ollama residency is read from /api/ps; resident_models must reflect
    only the running subset, not the full installed list."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", payload={"data": [
            {"id": "gemma-4-27b"},
            {"id": "llama3:8b"},
            {"id": "qwen3.6"},
        ]})
        m.get(f"{OLLAMA_URL}/api/ps", payload={"models": [
            {"name": "llama3:8b"},
        ]})
        m.get(f"{LMSTUDIO_URL}/v1/models", status=500)
        backends = await router.discover()

    assert len(backends) == 1
    ollama = backends[0]
    assert ollama.name == "ollama"
    assert sorted(ollama.models) == ["gemma-4-27b", "llama3:8b", "qwen3.6"]
    assert ollama.resident_models == ["llama3:8b"]


@pytest.mark.asyncio
async def test_discover_lmstudio_falls_back_to_installed_when_state_endpoint_unavailable():
    """When LM Studio's /api/v0/models is not exposed, the /v1/models list is
    used as the resident set (per the spec's documented assumption)."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", status=500)
        # LM Studio path: /api/v0/models returns 404 → fall back to /v1/models
        m.get(f"{LMSTUDIO_URL}/api/v0/models", status=404)
        m.get(f"{LMSTUDIO_URL}/v1/models", payload={"data": [
            {"id": "lmstudio-community/gemma-4-26b-GGUF"},
            {"id": "phi-4"},
        ]})
        backends = await router.discover()

    assert len(backends) == 1
    lms = backends[0]
    assert lms.name == "lmstudio"
    assert sorted(lms.resident_models) == sorted(lms.models)


@pytest.mark.asyncio
async def test_discover_ollama_ps_unavailable_falls_back_to_installed():
    """Ollama /api/ps unreachable (older versions) → resident set is the
    installed list. No crash, no exclusion."""
    router = LocalModelRouter()
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/v1/models", payload={"data": [{"id": "qwen3.6"}]})
        m.get(f"{OLLAMA_URL}/api/ps", status=404)
        m.get(f"{LMSTUDIO_URL}/v1/models", status=500)
        backends = await router.discover()

    assert len(backends) == 1
    ollama = backends[0]
    assert ollama.resident_models == ollama.models


# ─── chat() — per-request model override (Sprint 04 hardening) ───────────────

@pytest.mark.asyncio
async def test_chat_honors_explicit_model_override():
    """A caller-supplied model name goes out on the wire as-is, even when the
    static preference order would have picked a different model."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["gemma-4-27b", "gemma-4-26b", "qwen3.6"],
        resident_models=["gemma-4-27b"],
    )
    captured = {}

    def fake_post(url, *, json, **kwargs):
        captured["url"] = url
        captured["json"] = json
        # Return a streaming response with a single delta + DONE.
        async def _iter():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = _iter()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        chunks = []
        async for chunk in router.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="qwen3.6",
            backend=backend,
        ):
            chunks.append(chunk)

    assert chunks == ["hi"]
    assert captured["json"]["model"] == "qwen3.6"


@pytest.mark.asyncio
async def test_chat_uses_resident_preferred_when_no_override():
    """No override → router picks from the resident set per static order.
    The picked model is the resident gemma-4-27b, not the higher-preference
    but unloaded gemma-4-26b (which doesn't exist on this backend)."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["gemma-4-27b", "qwen3.6", "phi-4"],
        resident_models=["qwen3.6"],
    )
    captured = {}

    def fake_post(url, *, json, **kwargs):
        captured["json"] = json
        async def _iter():
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            yield b"data: [DONE]\n\n"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = _iter()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        async for _ in router.chat(
            messages=[{"role": "user", "content": "hi"}],
            model=None,
            backend=backend,
        ):
            pass

    # No gemma in resident, so the first resident (qwen3.6) wins.
    assert captured["json"]["model"] == "qwen3.6"


@pytest.mark.asyncio
async def test_chat_override_not_available_raises_clean_error():
    """An override that names a model the backend doesn't have must raise
    RuntimeError *before* the HTTP call. NO silent substitution. NO 400 from
    the backend leaking through."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["gemma-4-27b"],
        resident_models=["gemma-4-27b"],
    )

    # If the router erroneously called the backend with the wrong model,
    # this would explode because the URL is unregistered. We assert the
    # error is the clean RuntimeError, not a network failure.
    with pytest.raises(RuntimeError, match="not available on backend"):
        async for _ in router.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="not-installed-anywhere",
            backend=backend,
        ):
            pass


@pytest.mark.asyncio
async def test_chat_no_backend_available_raises_clean_error():
    """When discovery returns no backends at all, chat raises a clean
    RuntimeError that matches the spec's error pattern. The execute layer
    catches this and returns the user-facing '[No local backend ...]' string."""
    router = LocalModelRouter()
    router._cached_backends = []

    with aioresponses():
        # Neither ollama nor lmstudio responds
        with pytest.raises(RuntimeError, match="No local backend available"):
            async for _ in router.chat(
                messages=[{"role": "user", "content": "hi"}],
            ):
                pass


@pytest.mark.asyncio
async def test_chat_no_override_no_residency_falls_back_to_legacy_order():
    """When the backend exposes no residency info (resident_models == [] or
    matches models), the legacy static order is preserved — gemma-4-27b wins."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["llama3:8b", "gemma-4-27b", "phi-4"],
        resident_models=[],   # backend doesn't expose residency
    )
    captured = {}

    def fake_post(url, *, json, **kwargs):
        captured["json"] = json
        async def _iter():
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            yield b"data: [DONE]\n\n"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = _iter()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        async for _ in router.chat(
            messages=[{"role": "user", "content": "hi"}],
            model=None,
            backend=backend,
        ):
            pass

    assert captured["json"]["model"] == "gemma-4-27b"


# ─── chat() — empty-stream detection (task 12) ─────────────────────────────

@pytest.mark.asyncio
async def test_chat_empty_stream_raises_runtime_error():
    """Zero content chunks from the backend must raise RuntimeError
    with a message naming the model and the likely cause."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["qwen3.5-2b", "gemma-4-27b"],
        resident_models=["gemma-4-27b"],
    )

    def fake_post(url, *, json, **kwargs):
        # Simulate LM Studio returning [DONE] immediately — model advertised
        # but not loaded.
        async def _iter():
            yield b"data: [DONE]\n\n"
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = _iter()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with pytest.raises(RuntimeError, match="returned empty output"):
            chunks = []
            async for chunk in router.chat(
                messages=[{"role": "user", "content": "hello"}],
                model="qwen3.5-2b",
                backend=backend,
            ):
                chunks.append(chunk)


@pytest.mark.asyncio
async def test_chat_error_body_in_stream_raises_runtime_error():
    """Non-SSE JSON error body in a streaming response (e.g. LM Studio
    returning plain JSON instead of SSE for an unloaded model) must raise
    RuntimeError with the upstream error message."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["qwen3.5-2b"],
        resident_models=[],
    )

    def fake_post(url, *, json, **kwargs):
        async def _iter():
            yield b'{"error": "Model load failed: OOM"}'
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = _iter()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with pytest.raises(RuntimeError, match="OOM"):
            async for _ in router.chat(
                messages=[{"role": "user", "content": "hello"}],
                model="qwen3.5-2b",
                backend=backend,
            ):
                pass


@pytest.mark.asyncio
async def test_chat_non_stream_error_body_raises_runtime_error():
    """Non-streaming response with an error key raises RuntimeError."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["qwen3.5-2b"],
        resident_models=[],
    )

    def fake_post(url, *, json, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value={"error": "Model not loaded"})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with pytest.raises(RuntimeError, match="Model not loaded"):
            async for _ in router.chat(
                messages=[{"role": "user", "content": "hello"}],
                model="qwen3.5-2b",
                backend=backend,
                stream=False,
            ):
                pass


@pytest.mark.asyncio
async def test_chat_model_not_available_shows_resident_trimmed():
    """The 'not available' error must show resident models and hidden count,
    not dump all known models."""
    router = LocalModelRouter()
    backend = LocalBackendInfo(
        name="lmstudio", url=LMSTUDIO_URL,
        models=["qwen3.5-2b", "qwen3.5-4b", "gemma-4-27b", "phi-4"],
        resident_models=["gemma-4-27b"],
    )

    with pytest.raises(RuntimeError) as exc:
        async for _ in router.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="nonexistent",
            backend=backend,
        ):
            pass
    msg = str(exc.value)

    assert "resident: ['gemma-4-27b']" in msg
    assert "3 more installed" in msg
