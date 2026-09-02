"""Flight substrate Lane 1a — /api/flights core REST surface."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api.server import build_app
from core.config import config
from core.faces.registry import FaceRegistry
from core.flight import service as flight_service
from core.telemetry import spend as spend_mod

PROFILES_DIR = Path(__file__).parent.parent.parent / "core" / "faces" / "profiles"


class _FakeHashRedis:
    def __init__(self):
        self.h = {}

    async def hincrbyfloat(self, key, field, amount):
        self.h.setdefault(key, {})
        self.h[key][field] = self.h[key].get(field, 0.0) + float(amount)
        return self.h[key][field]

    async def hgetall(self, key):
        return {k: str(v) for k, v in self.h.get(key, {}).items()}

    async def delete(self, key):
        self.h.pop(key, None)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_DB", str(tmp_path / "flights.db"))
    flight_service._reset_for_tests()
    spend_redis = _FakeHashRedis()
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: spend_redis)
    spend_mod._LOCAL_SPEND.clear()
    yield spend_redis
    flight_service._reset_for_tests()


@pytest.fixture
async def client() -> Any:
    app = build_app(faces=FaceRegistry(profiles_dir=PROFILES_DIR))
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_create_get_and_budget(client, _isolate):
    resp = await client.post("/api/flights", json={
        "flight_id": "ms-5", "name": "Mega Sprint 5", "budget_usd": 2.0, "priority": 3,
    })
    assert resp.status == 201
    created = await resp.json()
    assert created["flight_id"] == "ms-5" and created["priority"] == 3

    # seed some spend, then the detail budget reflects it
    await spend_mod.record_flight_spend("ms-5", "kimi_platform", 0.5, emit=False)
    resp = await client.get("/api/flights/ms-5")
    assert resp.status == 200
    detail = await resp.json()
    assert detail["budget"]["spent_usd"] == 0.5
    assert detail["budget"]["remaining_usd"] == 1.5
    assert detail["budget"]["over_budget"] is False


async def test_create_duplicate_and_empty_id(client):
    await client.post("/api/flights", json={"flight_id": "a", "name": "A"})
    dup = await client.post("/api/flights", json={"flight_id": "a", "name": "A2"})
    assert dup.status == 400
    empty = await client.post("/api/flights", json={"flight_id": "  ", "name": "x"})
    assert empty.status == 400


async def test_get_unknown_404(client):
    assert (await client.get("/api/flights/nope")).status == 404


async def test_list_and_status_filter(client):
    await client.post("/api/flights", json={"flight_id": "a", "name": "A"})
    await client.post("/api/flights", json={"flight_id": "b", "name": "B"})
    await client.patch("/api/flights/b", json={"status": "done"})
    items = (await (await client.get("/api/flights")).json())["items"]
    assert {f["flight_id"] for f in items} == {"a", "b"}
    active = (await (await client.get("/api/flights?status=active")).json())["items"]
    assert {f["flight_id"] for f in active} == {"a"}


async def test_patch_update_and_errors(client):
    await client.post("/api/flights", json={"flight_id": "a", "name": "A", "priority": 0})
    ok = await client.patch("/api/flights/a", json={"priority": 7, "budget_usd": 9.0})
    assert ok.status == 200 and (await ok.json())["priority"] == 7
    bad = await client.patch("/api/flights/a", json={"status": "bogus"})
    assert bad.status == 400
    missing = await client.patch("/api/flights/nope", json={"priority": 1})
    assert missing.status == 404


async def test_delete(client):
    await client.post("/api/flights", json={"flight_id": "a", "name": "A"})
    assert (await client.delete("/api/flights/a")).status == 200
    assert (await client.delete("/api/flights/a")).status == 404
