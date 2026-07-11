from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.embedder import SlotResolvedEmbedder
from core.memory.exceptions import SlotMisconfigured
from core.memory.models import SlotResolution


def _resolver(
    *,
    query_instruction_template: str | None = None,
    doc_instruction_template: str | None = None,
    embedding_batch_size: int | None = None,
) -> MagicMock:
    resolver = MagicMock()
    resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-embedder-model",
        backend="lmstudio",
        endpoint="http://localhost:1234",
        embedding_dimension=2,
        query_instruction_template=query_instruction_template,
        doc_instruction_template=doc_instruction_template,
        embedding_batch_size=embedding_batch_size,
    )
    return resolver


def _post_response(body: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None

    async def _json(*args, **kwargs):
        return body

    response.json = _json
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


@pytest.mark.asyncio
async def test_query_and_doc_apply_distinct_configured_templates():
    embedder = SlotResolvedEmbedder(
        _resolver(query_instruction_template="query: {text}"),
        "embed_text",
    )
    response = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    with patch(
        "aiohttp.ClientSession.post",
        side_effect=[_post_response(response)] * 3,
    ) as post:
        doc = await embedder.embed_doc(["needle"])
        query = await embedder.embed_query(["needle"])
        legacy = await embedder.embed(["needle"])

    assert doc == [[0.1, 0.2]]
    assert query == [[0.1, 0.2]]
    assert legacy == doc
    assert [
        call.kwargs["json"]["input"]
        for call in post.call_args_list
    ] == [["needle"], ["query: needle"], ["needle"]]


@pytest.mark.asyncio
async def test_no_template_preserves_text_content_with_intended_list_wire_shape():
    embedder = SlotResolvedEmbedder(_resolver(), "embed_text")
    response = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    with patch(
        "aiohttp.ClientSession.post",
        side_effect=[_post_response(response)] * 3,
    ) as post:
        doc = await embedder.embed_doc(["legacy text"])
        query = await embedder.embed_query(["legacy text"])
        legacy = await embedder.embed(["legacy text"])

    assert doc == query == legacy == [[0.1, 0.2]]
    wire_inputs = [
        call.kwargs["json"]["input"]
        for call in post.call_args_list
    ]
    assert all(isinstance(batch, list) for batch in wire_inputs)
    assert wire_inputs == [["legacy text"], ["legacy text"], ["legacy text"]]
    assert [batch[0] for batch in wire_inputs] == ["legacy text"] * 3


@pytest.mark.asyncio
async def test_query_template_preserves_whitespace_only_input_contract():
    embedder = SlotResolvedEmbedder(
        _resolver(query_instruction_template="query: {text}"),
        "embed_text",
    )
    zero_vector = [0.0, 0.0]
    response = {"data": [{"index": 0, "embedding": zero_vector}]}

    with patch(
        "aiohttp.ClientSession.post",
        return_value=_post_response(response),
    ) as post:
        assert await embedder.embed_query(["   "]) == [zero_vector]

    assert post.call_args.kwargs["json"]["input"] == ["   "]


@pytest.mark.asyncio
async def test_lmstudio_batches_requests_and_restores_response_order():
    embedder = SlotResolvedEmbedder(_resolver(embedding_batch_size=2), "embed_text")
    responses = [
        {
            "data": [
                {"index": 1, "embedding": [0.2, 0.0]},
                {"index": 0, "embedding": [0.1, 0.0]},
            ]
        },
        {
            "data": [
                {"index": 1, "embedding": [0.4, 0.0]},
                {"index": 0, "embedding": [0.3, 0.0]},
            ]
        },
        {"data": [{"index": 0, "embedding": [0.5, 0.0]}]},
    ]

    with patch(
        "aiohttp.ClientSession.post",
        side_effect=[_post_response(body) for body in responses],
    ) as post:
        vectors = await embedder.embed_doc(["a", "b", "c", "d", "e"])

    assert vectors == [
        [0.1, 0.0],
        [0.2, 0.0],
        [0.3, 0.0],
        [0.4, 0.0],
        [0.5, 0.0],
    ]
    assert post.call_count == 3
    assert [
        call.kwargs["json"]["input"]
        for call in post.call_args_list
    ] == [["a", "b"], ["c", "d"], ["e"]]


@pytest.mark.asyncio
async def test_lmstudio_partial_batch_response_fails_whole_call():
    embedder = SlotResolvedEmbedder(_resolver(embedding_batch_size=2), "embed_text")
    partial = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    with patch(
        "aiohttp.ClientSession.post", return_value=_post_response(partial)):
        with pytest.raises(Exception, match="returned 1 vectors for 2 inputs"):
            await embedder.embed_doc(["a", "b"])


@pytest.mark.asyncio
async def test_empty_input_short_circuits_without_http_request():
    embedder = SlotResolvedEmbedder(_resolver(), "embed_text")

    with patch("aiohttp.ClientSession.post") as post:
        assert await embedder.embed_doc([]) == []

    post.assert_not_called()


def test_explicit_invalid_batch_size_is_rejected():
    with pytest.raises(SlotMisconfigured, match="embedding_batch_size"):
        SlotResolvedEmbedder(_resolver(embedding_batch_size=0), "embed_text")
