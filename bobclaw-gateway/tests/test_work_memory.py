"""Work-lane boundary tests: personal reads work, work never reads personal.

No network: the embedder/Qdrant seams are monkeypatched, so these assert the
authorization and ledger contract only.
"""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

WORK_SCOPE = {
    "may_touch": ["/var/lib/example/work/**"],
    "may_not_touch": ["/var/lib/example/family/**"],
}
DAD_SCOPE = {"may_touch": ["/var/lib/example/family/dad/**"], "may_not_touch": []}


def _app(module, claims, tmp_path):
    @web.middleware
    async def identity(request, handler):
        request["user"] = claims
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app.router.add_routes(module.router)
    return app


@pytest.fixture()
def lane(monkeypatch, tmp_path):
    from routers import work_memory as m

    monkeypatch.setitem(m.LANE, "root", "/var/lib/example/work")
    from pathlib import Path
    monkeypatch.setattr(m, "PERSONAL_READ_PATH", Path("/var/lib/example/family/dad/MEMORY.md"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("SPS proprietary zebra thermal note", encoding="utf-8")
    monkeypatch.setitem(m.LANE, "ledger", tmp_path / "work_notes.jsonl")
    monkeypatch.setitem(m.LANE, "docs", docs)
    monkeypatch.setattr(m, "_embed", lambda text: [0.1] * m.DIM)
    monkeypatch.setattr(m, "_search_points", lambda vector, limit: [])
    monkeypatch.setattr(m, "_upsert_note", lambda entry: "point-" + entry["note_id"])
    return m


@pytest.mark.asyncio
async def test_work_scope_can_write_and_read_back(lane):
    claims = {"sub": "work", "token_type": "agent", "scope": dict(WORK_SCOPE)}
    async with TestClient(TestServer(_app(lane, claims, None))) as c:
        r = await c.get("/work-memory/status")
        assert r.status == 200
        assert (await r.json())["caller_can_write"] is True

        r = await c.post("/work-memory/save", json={"content": "line one zebra", "category": "fact"})
        assert r.status == 201
        saved = await r.json()
        assert saved["indexed"] is True

        r = await c.post("/work-memory/read", json={"source_id": saved["source_id"]})
        assert r.status == 200
        assert "line one zebra" in (await r.json())["content"]

        # literal fallback finds the note even with semantic search stubbed out
        r = await c.post("/work-memory/search", json={"query": "zebra"})
        items = (await r.json())["items"]
        assert any(i["kind"] == "work_note" for i in items)


@pytest.mark.asyncio
async def test_personal_can_read_but_never_write(lane):
    claims = {"sub": "admin", "token_type": "agent", "scope": dict(DAD_SCOPE)}
    async with TestClient(TestServer(_app(lane, claims, None))) as c:
        r = await c.post("/work-memory/search", json={"query": "zebra"})
        assert r.status == 200
        assert (await r.json())["coverage"]["caller_can_write"] is False

        r = await c.post("/work-memory/save", json={"content": "personal must not write"})
        assert r.status == 403


@pytest.mark.asyncio
async def test_no_scope_and_foreign_ids_are_refused(lane):
    claims = {"sub": "nobody", "token_type": "agent"}
    async with TestClient(TestServer(_app(lane, claims, None))) as c:
        assert (await c.get("/work-memory/status")).status == 403
        assert (await c.post("/work-memory/search", json={"query": "x"})).status == 403
        assert (await c.post("/work-memory/save", json={"content": "x"})).status == 403
        assert (await c.post("/work-memory/read", json={"source_id": "sps:note:abc"})).status == 403

    claims = {"sub": "work", "token_type": "agent", "scope": dict(WORK_SCOPE)}
    async with TestClient(TestServer(_app(lane, claims, None))) as c:
        for sid in ("family:dad:MEMORY.md:0", "event:abc", "sps:chunk:not-a-uuid"):
            r = await c.post("/work-memory/read", json={"source_id": sid})
            assert r.status == 404, sid


@pytest.mark.asyncio
async def test_unconfigured_work_lane_is_unavailable(lane, monkeypatch):
    monkeypatch.setitem(lane.LANE, "root", None)
    claims = {"sub": "work", "token_type": "agent", "scope": {"may_touch": ["**"]}}
    assert not lane.can_write({"user": claims})
    async with TestClient(TestServer(_app(lane, claims, None))) as c:
        for method, route in [("get", "status"), ("post", "search"), ("post", "read"), ("post", "save")]:
            response = await getattr(c, method)("/work-memory/" + route)
            assert response.status == 503
            assert "set BOB_SPS_LANE_ROOT" in await response.text()
