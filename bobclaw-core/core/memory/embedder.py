from __future__ import annotations

import logging

import aiohttp
from aiohttp import ClientConnectorError

from core.memory.exceptions import EmbedderUnavailable, SlotMisconfigured
from core.memory.slots import SlotResolver

log = logging.getLogger(__name__)

_BACKEND_LMSTUDIO = "lmstudio"
_BACKEND_OLLAMA = "ollama"


class SlotResolvedEmbedder:
    def __init__(self, slot_resolver: SlotResolver, slot_name: str = "embed_text") -> None:
        resolution = slot_resolver.get(slot_name)
        if resolution.embedding_dimension is None:
            raise SlotMisconfigured(
                slot_name, "embedding_dimension is missing in slot config"
            )
        if resolution.backend not in (_BACKEND_LMSTUDIO, _BACKEND_OLLAMA):
            raise SlotMisconfigured(
                slot_name, f"unsupported embedder backend: {resolution.backend}"
            )
        self._resolution = resolution
        self.embedding_dimension = resolution.embedding_dimension
        self._backend = resolution.backend
        self._endpoint = resolution.endpoint.rstrip("/")
        self._model = resolution.model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            vec = await self._embed_one(text)
            results.append(vec)
        self._guard_batch(texts, results)
        return results

    def _guard_batch(self, texts: list[str], vectors: list[list[float]]) -> None:
        """Guard against zero/degenerate vectors + dim/count mismatch (2026-06-20 zero-vector
        incident; LKS ``EmbedClient._do_embed`` parity). Inspects only — healthy (non-zero,
        correct-dim) vectors flow through ``embed`` byte-identical; the guard fires solely on the
        degenerate / dim-mismatch / count-mismatch cases."""
        # 1. batch count guard (mirror LKS)
        if len(vectors) != len(texts):
            raise EmbedderUnavailable(
                self._endpoint,
                f"returned {len(vectors)} vectors for {len(texts)} inputs",
            )
        # 2. per-vector checks; empty/whitespace input keeps the current contract
        #    (LKS guards only non-empty text)
        for text, vec in zip(texts, vectors):
            if not text.strip():
                continue
            # 2a. zero / degenerate (mirror LKS message + 1e-9 threshold)
            if not vec or not any(abs(x) > 1e-9 for x in vec):
                raise EmbedderUnavailable(
                    self._endpoint,
                    "returned a zero/degenerate vector for non-empty text — likely a "
                    "half-loaded or wrong model. Refusing to index (would silently break "
                    "retrieval). Restart the embedder slot and verify.",
                )
            # 2b. dimension check at the embedder (not only post-hoc in the indexer)
            if len(vec) != self.embedding_dimension:
                raise EmbedderUnavailable(
                    self._endpoint,
                    f"returned a {len(vec)}-dim vector, expected {self.embedding_dimension} — "
                    "likely a wrong/half-loaded model. Refusing to index.",
                )

    async def _embed_one(self, text: str) -> list[float]:
        if self._backend == _BACKEND_LMSTUDIO:
            return await self._embed_via_lmstudio(text)
        return await self._embed_via_ollama(text)

    async def _embed_via_lmstudio(self, text: str) -> list[float]:
        payload = {"model": self._model, "input": text}
        url = f"{self._endpoint}/v1/embeddings"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
        except ClientConnectorError as exc:
            raise EmbedderUnavailable(
                self._endpoint, f"{_BACKEND_LMSTUDIO} unreachable at {url}: {exc}"
            ) from exc
        except Exception as exc:
            raise EmbedderUnavailable(
                self._endpoint,
                f"{_BACKEND_LMSTUDIO} embedding call failed at {url}: {exc}",
            ) from exc
        return self._parse_lmstudio_response(body, url)

    async def _embed_via_ollama(self, text: str) -> list[float]:
        payload = {"model": self._model, "prompt": text}
        url = f"{self._endpoint}/api/embeddings"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
        except ClientConnectorError as exc:
            raise EmbedderUnavailable(
                self._endpoint, f"{_BACKEND_OLLAMA} unreachable at {url}: {exc}"
            ) from exc
        except Exception as exc:
            raise EmbedderUnavailable(
                self._endpoint,
                f"{_BACKEND_OLLAMA} embedding call failed at {url}: {exc}",
            ) from exc
        return self._parse_ollama_response(body, url)

    @staticmethod
    def _parse_lmstudio_response(body: dict, url: str) -> list[float]:
        try:
            data = body["data"]
            embedding = data[0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbedderUnavailable(
                url, f"unexpected {_BACKEND_LMSTUDIO} response shape: {exc}"
            ) from exc
        return embedding

    @staticmethod
    def _parse_ollama_response(body: dict, url: str) -> list[float]:
        try:
            embedding = body["embedding"]
        except (KeyError, TypeError) as exc:
            raise EmbedderUnavailable(
                url, f"unexpected {_BACKEND_OLLAMA} response shape: {exc}"
            ) from exc
        return embedding
