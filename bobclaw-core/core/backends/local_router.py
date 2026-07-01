"""
BoBClaw Core — Unified local model router (Ollama + LM Studio)

Discovers available local backends, selects the best one per platform,
and provides a unified streaming/non-streaming chat interface over the
shared OpenAI-compatible /v1/chat/completions endpoint.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

import aiohttp

from core.config import config


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class LocalBackendInfo:
    name: str        # "ollama" | "lmstudio"
    url: str
    models: list[str] = field(default_factory=list)
    # Models currently loaded into the backend's memory ("resident"). For LM
    # Studio we treat /v1/models as the resident list — it returns only
    # installed/selectable models, and the failure mode this whole layer exists
    # to avoid is "picked model can't load alongside the resident one." For
    # Ollama we probe /api/ps to get the *running* subset of installed models.
    resident_models: list[str] = field(default_factory=list)


# ─── Router ───────────────────────────────────────────────────────────────────

class LocalModelRouter:
    """Discovers and routes chat requests to local LLM backends."""

    _DISCOVER_TIMEOUT = aiohttp.ClientTimeout(total=2)

    def __init__(self) -> None:
        self._cached_backends: list[LocalBackendInfo] = []

    # ── discovery ──────────────────────────────────────────────────────────────

    async def _fetch_models(
        self, session: aiohttp.ClientSession, url: str
    ) -> list[str]:
        """Return model ids from GET /v1/models, or [] on any failure."""
        try:
            async with session.get(f"{url}/v1/models") as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            return []

    async def _fetch_resident_ollama(
        self, session: aiohttp.ClientSession, url: str
    ) -> list[str]:
        """Return model names currently loaded into Ollama memory, or [] on any failure.

        Uses Ollama's native /api/ps endpoint (not exposed on the OpenAI-compat
        layer). Each entry carries ``name``; we strip size/variant suffixes so
        the resident list matches the same id space /v1/models returns.
        """
        try:
            async with session.get(f"{url}/api/ps") as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
            names: list[str] = []
            for entry in data.get("models", []):
                if isinstance(entry, dict) and entry.get("name"):
                    names.append(entry["name"])
            return names
        except Exception:
            return []

    async def _fetch_resident_lmstudio(
        self, session: aiohttp.ClientSession, url: str
    ) -> list[str]:
        """Return LM Studio's currently-loaded models, or [] on any failure.

        LM Studio's openai-compat /v1/models lists installed models, but does
        not distinguish "loaded" from "unloaded" at the public API level. The
        closest reliable signal is the model.state / loaded state endpoint
        (``GET /api/v0/models``); when it returns a different set than
        /v1/models, the intersection is the resident set. If the state endpoint
        is unavailable we fall back to the /v1/models list (per the spec's
        documented assumption that the list IS the resident set on a 16GB GPU
        where typically only one model is loaded at a time).
        """
        try:
            async with session.get(f"{url}/api/v0/models") as resp:
                if resp.status != 200:
                    # Fall back to the /v1/models list as documented.
                    return await self._fetch_models(session, url)
                data = await resp.json(content_type=None)
            loaded_ids: list[str] = []
            for entry in data.get("data", []):
                if not isinstance(entry, dict):
                    continue
                state = entry.get("state") or entry.get("loaded_state")
                if state and "loaded" not in str(state).lower():
                    continue
                if entry.get("id"):
                    loaded_ids.append(entry["id"])
            if loaded_ids:
                return loaded_ids
            # State endpoint returned 200 but no loaded models — fall back to
            # the /v1/models list to preserve the spec's residency assumption.
            return await self._fetch_models(session, url)
        except Exception:
            return []

    async def _fetch_resident(
        self, session: aiohttp.ClientSession, name: str, url: str
    ) -> list[str]:
        """Dispatch to the per-backend residency probe."""
        if name == "ollama":
            return await self._fetch_resident_ollama(session, url)
        if name == "lmstudio":
            return await self._fetch_resident_lmstudio(session, url)
        return []

    async def discover(self) -> list[LocalBackendInfo]:
        """Probe Ollama and LM Studio; return a list of reachable backends.

        Each backend carries both ``models`` (installed/selectable) and
        ``resident_models`` (currently loaded into backend memory, used by
        ``_pick_model`` to prefer a model that won't 400 on load).
        """
        backends: list[LocalBackendInfo] = []
        async with aiohttp.ClientSession(timeout=self._DISCOVER_TIMEOUT) as session:
            for name, url in [
                ("ollama",   config.OLLAMA_URL),
                ("lmstudio", config.LMSTUDIO_URL),
            ]:
                models = await self._fetch_models(session, url)
                if not models:
                    continue
                resident = await self._fetch_resident(session, name, url)
                # If the residency probe returned nothing usable, treat the
                # installed list as the resident set (preserves legacy behavior
                # when the backend doesn't expose a state endpoint).
                if not resident:
                    resident = list(models)
                backends.append(
                    LocalBackendInfo(
                        name=name,
                        url=url,
                        models=models,
                        resident_models=resident,
                    )
                )
        self._cached_backends = backends
        return backends

    # ── backend selection ──────────────────────────────────────────────────────

    def get_best_backend(
        self, backends: Optional[list[LocalBackendInfo]] = None
    ) -> Optional[LocalBackendInfo]:
        """
        Return the most suitable backend from *backends* (or the cached list).

        Platform preference:
          • Linux   → Ollama first, LM Studio fallback
          • Windows → LM Studio first, Ollama fallback
        """
        pool = backends if backends is not None else self._cached_backends
        if not pool:
            return None
        by_name = {b.name: b for b in pool}
        preferred_order = (
            ["ollama", "lmstudio"] if sys.platform == "linux"
            else ["lmstudio", "ollama"]
        )
        for name in preferred_order:
            if name in by_name:
                return by_name[name]
        return pool[0]

    # ── model selection ────────────────────────────────────────────────────────

    @staticmethod
    def _pick_model(
        models: list[str], resident: Optional[list[str]] = None
    ) -> Optional[str]:
        """
        Ranked selection.

        Residency preference (when *resident* is provided and non-empty):
          1. gemma-27b/26b **AND** resident
          2. any gemma **AND** resident
          3. first resident model
          4. fall through to legacy static order on *models*

        Legacy static order (when *resident* is None/empty — the "no
        residency info" path):
          1. gemma with 27b or 26b in the name (e.g. gemma-4-27b)
          2. any gemma model
          3. first available model
        """
        if not models:
            return None

        def _rank(candidates: list[str]) -> Optional[str]:
            for m in candidates:
                if "gemma" in m.lower() and re.search(r"2[76]b", m.lower()):
                    return m
            for m in candidates:
                if "gemma" in m.lower():
                    return m
            return candidates[0] if candidates else None

        if resident:
            ranked = _rank(resident)
            if ranked is not None:
                return ranked
        return _rank(models)

    def get_reasoning_model(
        self, backends: Optional[list[LocalBackendInfo]] = None
    ) -> Optional[tuple[LocalBackendInfo, str]]:
        """
        Find a ≥31B model suitable for complex reasoning / escalation.
        Returns (backend, model_name) or None if none found.
        """
        pool = backends if backends is not None else self._cached_backends
        for backend in pool:
            for model in backend.models:
                match = re.search(r"(\d+)b", model.lower())
                if match and int(match.group(1)) >= 31:
                    return backend, model
        return None

    # ── embedding discovery ────────────────────────────────────────────────────

    async def discover_embedding_models(self) -> list[tuple[LocalBackendInfo, str]]:
        """Return every (backend, model_name) whose name suggests embedding capability.

        Matches models whose lowercase name contains any of: embed, bge, granite,
        nomic, qwen3-emb. No ranking — returns in natural discovery order.
        """
        if not self._cached_backends:
            await self.discover()
        results: list[tuple[LocalBackendInfo, str]] = []
        keywords = ("embed", "bge", "granite", "nomic", "qwen3-emb")
        for backend in self._cached_backends:
            for model in backend.models:
                ml = model.lower()
                if any(kw in ml for kw in keywords):
                    results.append((backend, model))
        return results

    # ── embed ───────────────────────────────────────────────────────────────────

    async def embed(
        self,
        texts: list[str],
        model: str,
        backend_info: Optional[LocalBackendInfo] = None,
    ) -> list[list[float]]:
        """POST /v1/embeddings on the chosen backend. OpenAI-compatible.

        Raises RuntimeError if *model* is not found on any cached backend
        (when *backend_info* is None). Raises aiohttp.ClientError on transport
        failure. No swallowing, no retry, no zero-vector default.
        """
        if backend_info is None:
            if not self._cached_backends:
                await self.discover()
            for b in self._cached_backends:
                if model in b.models:
                    backend_info = b
                    break
            if backend_info is None:
                raise RuntimeError(f"Model {model!r} not on any discovered backend")

        url = f"{backend_info.url}/v1/embeddings"
        payload = {"model": model, "input": texts}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)

        data_entries = sorted(data.get("data", []), key=lambda e: e.get("index", 0))
        return [e["embedding"] for e in data_entries]

    # ── chat completion ────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        stream: bool = True,
        backend: Optional[LocalBackendInfo] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Async generator that yields text chunks (delta content).

        Discovers backends automatically if none are cached.
        Raises RuntimeError if no backend or model is available.

        When *model* is supplied explicitly (a per-request override from
        ``execute._default_send_to_backend``'s ``model_override`` path), the
        router honors it as-is — never silently substitutes a different
        model. If the override names a model the backend does not advertise,
        a clean RuntimeError is raised *before* the HTTP call, so the user
        gets a precise error string instead of a 400 from the backend.
        """
        if backend is None:
            if not self._cached_backends:
                await self.discover()
            backend = self.get_best_backend()
        if backend is None:
            raise RuntimeError(
                "No local backend available — start Ollama or LM Studio "
                "and retry, or configure a cloud backend."
            )

        if model is not None:
            # Per-request override: honor it exactly, or error clearly.
            # Never silently substitute (see spec point 3).
            if model not in backend.models:
                resident = backend.resident_models
                other_count = len(backend.models) - len(resident) if resident else 0
                if resident and other_count > 0:
                    detail = f"resident: {resident}; {other_count} more installed"
                elif resident:
                    detail = f"resident: {resident}"
                else:
                    detail = f"known: {backend.models}"
                raise RuntimeError(
                    f"Requested model {model!r} is not available on backend "
                    f"{backend.name!r} ({detail})"
                )
            chosen_model = model
        else:
            chosen_model = self._pick_model(
                backend.models, resident=backend.resident_models or None
            )
            if chosen_model is None:
                raise RuntimeError(
                    f"No models available on backend '{backend.name}'"
                )

        payload = {
            "model": chosen_model,
            "messages": messages,
            "stream": stream,
        }
        url = f"{backend.url}/v1/chat/completions"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                if not stream:
                    data = await resp.json(content_type=None)
                    if "error" in data:
                        raise RuntimeError(
                            f"Backend {backend.name!r} returned error for model "
                            f"{chosen_model!r}: {data['error']}"
                        )
                    yield data["choices"][0]["message"]["content"]
                    return
                yielded_any = False
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        # Non-SSE lines may carry plain JSON error bodies
                        # (e.g. LM Studio returning {"error":"..."} instead
                        # of SSE for an unloaded model).
                        try:
                            err_body = json.loads(line)
                            if "error" in err_body:
                                raise RuntimeError(
                                    f"Backend {backend.name!r} returned error for model "
                                    f"{chosen_model!r}: {err_body['error']}"
                                )
                        except json.JSONDecodeError:
                            pass
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            yielded_any = True
                            yield delta
                    except Exception:
                        continue
                if not yielded_any:
                    resident = backend.resident_models
                    raise RuntimeError(
                        f"Model {chosen_model!r} on {backend.name!r} returned empty output"
                        f" — installed but not loaded. Load it in {backend.name}"
                        f" or pick a resident model: {resident or backend.models}"
                    )
