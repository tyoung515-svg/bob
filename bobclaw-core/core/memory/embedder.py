from __future__ import annotations

import logging

import aiohttp
from aiohttp import ClientConnectorError

from core.memory.exceptions import EmbedderUnavailable, SlotMisconfigured
from core.memory.slots import SlotResolver

log = logging.getLogger(__name__)

_BACKEND_LMSTUDIO = "lmstudio"
_BACKEND_OLLAMA = "ollama"
_DEFAULT_EMBEDDING_BATCH_SIZE = 64


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
        batch_size = (
            resolution.embedding_batch_size
            if resolution.embedding_batch_size is not None
            else _DEFAULT_EMBEDDING_BATCH_SIZE
        )
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise SlotMisconfigured(
                slot_name,
                "embedding_batch_size must be a positive integer when configured",
            )
        self._resolution = resolution
        self.embedding_dimension = resolution.embedding_dimension
        self._backend = resolution.backend
        self._endpoint = resolution.endpoint.rstrip("/")
        self._model = resolution.model
        self._query_instruction_template = resolution.query_instruction_template
        self._doc_instruction_template = resolution.doc_instruction_template
        self._embedding_batch_size = batch_size

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Deprecated symmetric alias; delegates to embed_doc for one release."""
        return await self.embed_doc(texts)

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, self._query_instruction_template)

    async def embed_doc(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, self._doc_instruction_template)

    async def _embed(
        self,
        texts: list[str],
        instruction_template: str | None,
    ) -> list[list[float]]:
        if not texts:
            return []
        request_texts = [
            self._apply_instruction_template(text, instruction_template)
            for text in texts
        ]
        if self._backend == _BACKEND_LMSTUDIO:
            vectors = await self._embed_lmstudio_batches(request_texts)
        else:
            vectors = await self._embed_ollama_texts(request_texts)
        self._guard_batch(request_texts, vectors)
        return vectors

    @staticmethod
    def _apply_instruction_template(text: str, template: str | None) -> str:
        if template is None:
            return text
        if "{text}" in template:
            return template.replace("{text}", text)
        return f"{template}{text}"

    async def _embed_lmstudio_batches(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._endpoint}/v1/embeddings"
        vectors: list[list[float]] = []
        try:
            async with aiohttp.ClientSession() as session:
                for start in range(0, len(texts), self._embedding_batch_size):
                    batch = texts[start : start + self._embedding_batch_size]
                    vectors.extend(await self._embed_via_lmstudio(session, url, batch))
        except ClientConnectorError as exc:
            raise EmbedderUnavailable(
                self._endpoint, f"{_BACKEND_LMSTUDIO} unreachable at {url}: {exc}"
            ) from exc
        except EmbedderUnavailable:
            raise
        except Exception as exc:
            raise EmbedderUnavailable(
                self._endpoint,
                f"{_BACKEND_LMSTUDIO} embedding call failed at {url}: {exc}",
            ) from exc
        return vectors

    async def _embed_ollama_texts(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._endpoint}/api/embeddings"
        vectors: list[list[float]] = []
        try:
            async with aiohttp.ClientSession() as session:
                for text in texts:
                    vectors.append(await self._embed_via_ollama(session, url, text))
        except ClientConnectorError as exc:
            raise EmbedderUnavailable(
                self._endpoint, f"{_BACKEND_OLLAMA} unreachable at {url}: {exc}"
            ) from exc
        except EmbedderUnavailable:
            raise
        except Exception as exc:
            raise EmbedderUnavailable(
                self._endpoint,
                f"{_BACKEND_OLLAMA} embedding call failed at {url}: {exc}",
            ) from exc
        return vectors

    async def _embed_via_lmstudio(
        self,
        session: aiohttp.ClientSession,
        url: str,
        texts: list[str],
    ) -> list[list[float]]:
        payload = {"model": self._model, "input": texts}
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        return self._parse_lmstudio_response(body, url, expected_count=len(texts))

    async def _embed_via_ollama(
        self,
        session: aiohttp.ClientSession,
        url: str,
        text: str,
    ) -> list[float]:
        payload = {"model": self._model, "prompt": text}
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        return self._parse_ollama_response(body, url)

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

    @staticmethod
    def _parse_lmstudio_response(
        body: dict,
        url: str,
        *,
        expected_count: int,
    ) -> list[list[float]]:
        try:
            data = body["data"]
        except (KeyError, TypeError) as exc:
            raise EmbedderUnavailable(
                url, f"unexpected {_BACKEND_LMSTUDIO} response shape: {exc}"
            ) from exc
        if not isinstance(data, list) or len(data) != expected_count:
            actual = len(data) if isinstance(data, list) else "non-list"
            raise EmbedderUnavailable(
                url,
                f"returned {actual} vectors for {expected_count} inputs",
            )

        vectors_by_index: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbedderUnavailable(
                    url,
                    f"unexpected {_BACKEND_LMSTUDIO} response item: {item!r}",
                )
            index = item.get("index")
            embedding = item.get("embedding")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= expected_count
                or index in vectors_by_index
                or not isinstance(embedding, list)
            ):
                raise EmbedderUnavailable(
                    url,
                    f"unexpected {_BACKEND_LMSTUDIO} response item: {item!r}",
                )
            vectors_by_index[index] = embedding
        if len(vectors_by_index) != expected_count:
            raise EmbedderUnavailable(
                url,
                f"returned {len(vectors_by_index)} vectors for {expected_count} inputs",
            )
        return [vectors_by_index[index] for index in range(expected_count)]

    @staticmethod
    def _parse_ollama_response(body: dict, url: str) -> list[float]:
        try:
            embedding = body["embedding"]
        except (KeyError, TypeError) as exc:
            raise EmbedderUnavailable(
                url, f"unexpected {_BACKEND_OLLAMA} response shape: {exc}"
            ) from exc
        return embedding
