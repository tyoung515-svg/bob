"""Approval ids survive failed graph resumes and remain single-use on success.

The graph is a local async-generator stub; no checkpoint service or model
backend is contacted.
"""
from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api.server import APPROVALS_KEY, build_app


def _sse_events(body: bytes) -> list[dict]:
    events = []
    for block in body.decode("utf-8").split("\n\n"):
        if block.startswith("data:"):
            events.append(json.loads(block[5:].strip()))
    return events


class _FailOnceGraph:
    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, graph_input, config, stream_mode):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("checkpoint resume failed")
        if False:  # make this an async generator on the successful empty run
            yield None


@pytest.mark.asyncio
async def test_failed_resume_rearms_id_then_success_consumes_it():
    graph = _FailOnceGraph()
    app = build_app(graph=graph)
    approval_id = "retry-safe-id"
    app[APPROVALS_KEY][approval_id] = "conversation:thread"

    async with TestClient(TestServer(app)) as client:
        failed = await client.post(
            "/api/chat/approval",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert failed.status == 200
        failed_events = _sse_events(await failed.read())
        assert any(
            event.get("type") == "error"
            and event.get("code") == "stream_error"
            for event in failed_events
        )
        assert not any(
            event.get("type") == "message_complete"
            for event in failed_events
        )
        assert app[APPROVALS_KEY][approval_id] == "conversation:thread"

        retried = await client.post(
            "/api/chat/approval",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert retried.status == 200
        retried_events = _sse_events(await retried.read())
        assert any(
            event.get("type") == "message_complete"
            for event in retried_events
        )
        assert approval_id not in app[APPROVALS_KEY]

        replay = await client.post(
            "/api/chat/approval",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert replay.status == 404
        assert (await replay.json())["code"] == "approval_not_found"
