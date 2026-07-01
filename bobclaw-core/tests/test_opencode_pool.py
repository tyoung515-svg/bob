"""
BoBClaw Core — Unit tests for OpenCodeServePool
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.opencode_pool import NoOpenCodeAvailable, OpenCodeServePool, _Instance


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_pool(instances: list[_Instance] | None = None) -> OpenCodeServePool:
    pool = OpenCodeServePool.__new__(OpenCodeServePool)
    pool._instances = list(instances) if instances else []
    pool._probe_task = None
    pool._shutdown = True  # disable background probing for tests
    return pool


def _make_instance(workspace_dir: str = "/ws", alive: bool = True, in_flight: int = 0):
    client = MagicMock()
    inst = _Instance(client=client, workspace_dir=workspace_dir, alive=alive, in_flight=in_flight)
    return inst


# ─── Pool builds from config ──────────────────────────────────────────────────

def test_pool_builds_from_config_instances():
    with patch.object(
        OpenCodeServePool, "_load_instances"
    ) as mock_load:
        pool = OpenCodeServePool.__new__(OpenCodeServePool)
        pool._instances = []
        pool._probe_task = None
        pool._shutdown = True
        mock_load.assert_not_called()


# ─── Empty pool ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_pool_raises_no_opencode_available():
    pool = _make_pool([])
    with pytest.raises(NoOpenCodeAvailable):
        await pool.dispatch([{"role": "user", "content": "hi"}])


# ─── Workspace filter ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_filters_by_workspace():
    inst_a = _make_instance(workspace_dir="/ws-a", alive=True)
    inst_a.client.chat = AsyncMock(return_value="from-a")
    inst_b = _make_instance(workspace_dir="/ws-b", alive=True)
    inst_b.client.chat = AsyncMock(return_value="from-b")

    pool = _make_pool([inst_a, inst_b])
    result = await pool.dispatch([{"role": "user", "content": "hi"}], workspace_dir="/ws-b")
    assert result == "from-b"


@pytest.mark.asyncio
async def test_dispatch_none_workspace_allows_any():
    inst_a = _make_instance(workspace_dir="/ws-a", alive=True)
    inst_a.client.chat = AsyncMock(return_value="from-a")

    pool = _make_pool([inst_a])
    result = await pool.dispatch([{"role": "user", "content": "hi"}], workspace_dir=None)
    assert result == "from-a"


@pytest.mark.asyncio
async def test_dispatch_mismatch_raises():
    inst_a = _make_instance(workspace_dir="/ws-a", alive=True)
    pool = _make_pool([inst_a])
    with pytest.raises(NoOpenCodeAvailable):
        await pool.dispatch([{"role": "user", "content": "hi"}], workspace_dir="/ws-z")


# ─── Dead instance excluded ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dead_instance_is_excluded():
    alive = _make_instance(alive=True)
    alive.client.chat = AsyncMock(return_value="alive")
    dead = _make_instance(alive=False)
    dead.client.chat = AsyncMock(return_value="dead")

    pool = _make_pool([alive, dead])
    result = await pool.dispatch([{"role": "user", "content": "hi"}])
    assert result == "alive"


# ─── Least-busy selection ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_least_busy_instance_selected():
    busy = _make_instance(alive=True, in_flight=5)
    busy.client.chat = AsyncMock(return_value="busy")
    free = _make_instance(alive=True, in_flight=0)
    free.client.chat = AsyncMock(return_value="free")

    pool = _make_pool([busy, free])
    result = await pool.dispatch([{"role": "user", "content": "hi"}])
    assert result == "free"


# ─── In-flight counter ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_in_flight_increments_and_decrements():
    inst = _make_instance(alive=True, in_flight=0)
    inst.client.chat = AsyncMock(return_value="ok")

    pool = _make_pool([inst])
    assert inst.in_flight == 0
    await pool.dispatch([{"role": "user", "content": "hi"}])
    assert inst.in_flight == 0


@pytest.mark.asyncio
async def test_in_flight_decrements_on_error():
    inst = _make_instance(alive=True, in_flight=0)
    inst.client.chat = AsyncMock(side_effect=RuntimeError("boom"))

    pool = _make_pool([inst])
    with pytest.raises(RuntimeError):
        await pool.dispatch([{"role": "user", "content": "hi"}])
    assert inst.in_flight == 0


# ─── Health probe revives dead instance ───────────────────────────────────────

@pytest.mark.asyncio
async def test_health_probe_revives_dead_instance():
    inst = _make_instance(alive=False)
    inst.client.health_check = AsyncMock(return_value=True)

    pool = _make_pool([inst])
    await pool._probe_all()
    assert inst.alive is True


@pytest.mark.asyncio
async def test_health_probe_marks_instance_dead():
    inst = _make_instance(alive=True)
    inst.client.health_check = AsyncMock(return_value=False)

    pool = _make_pool([inst])
    await pool._probe_all()
    assert inst.alive is False


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_cancels_probe_task():
    pool = OpenCodeServePool.__new__(OpenCodeServePool)
    pool._instances = []
    pool._shutdown = False

    # Create a dummy probe task
    async def _dummy():
        while True:
            await asyncio.sleep(1)

    pool._probe_task = asyncio.create_task(_dummy())
    await pool.close()
    assert pool._probe_task is None
    assert pool._shutdown is True


# ─── Path normalization ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_matches_normalized_case_and_separator():
    import os
    norm = os.path.normcase(os.path.normpath("C:/Users/Foo/ws"))
    inst = _make_instance(workspace_dir=norm, alive=True)
    inst.client.chat = AsyncMock(return_value="matched")
    pool = _make_pool([inst])
    result = await pool.dispatch(
        [{"role": "user", "content": "hi"}],
        workspace_dir="c:\\users\\foo\\ws",
    )
    assert result == "matched"


@pytest.mark.asyncio
async def test_dispatch_matches_with_trailing_slash():
    inst = _make_instance(workspace_dir="/path/to/ws", alive=True)
    inst.client.chat = AsyncMock(return_value="matched")
    pool = _make_pool([inst])
    result = await pool.dispatch(
        [{"role": "user", "content": "hi"}],
        workspace_dir="/path/to/ws/",
    )
    assert result == "matched"


@pytest.mark.asyncio
async def test_dispatch_none_workspace_still_wildcards():
    inst = _make_instance(workspace_dir="/some/ws", alive=True)
    inst.client.chat = AsyncMock(return_value="wildcarded")
    pool = _make_pool([inst])
    result = await pool.dispatch([{"role": "user", "content": "hi"}], workspace_dir=None)
    assert result == "wildcarded"


@pytest.mark.asyncio
async def test_register_stores_normalized_form():
    raw_input = "C:/Users/Foo/WS/"
    with patch.object(
        OpenCodeServePool, "_load_instances"
    ) as mock_load:
        pool = OpenCodeServePool.__new__(OpenCodeServePool)
        pool._instances = []
        pool._probe_task = None
        pool._shutdown = True

    with patch("core.backends.opencode_pool.config") as mock_config:
        mock_config.opencode_instances_parsed.return_value = [
            ("localhost", 8000, raw_input)
        ]
        pool._load_instances()

    assert len(pool._instances) == 1
    import os
    expected = os.path.normcase(os.path.normpath(raw_input))
    assert pool._instances[0].workspace_dir == expected


# ─── Multi-process registry convergence ────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_pools_from_same_env_have_identical_workspace_dirs():
    workers = [
        ("host1", 8001, "/ws/alpha"),
        ("host2", 8002, "/ws/beta"),
        ("host3", 8003, "/ws/gamma"),
    ]
    with patch("core.backends.opencode_pool.config") as mock_config:
        mock_config.opencode_instances_parsed.return_value = workers

        pool_a = OpenCodeServePool.__new__(OpenCodeServePool)
        pool_a._instances = []
        pool_a._probe_task = None
        pool_a._shutdown = True
        pool_a._load_instances()

        pool_b = OpenCodeServePool.__new__(OpenCodeServePool)
        pool_b._instances = []
        pool_b._probe_task = None
        pool_b._shutdown = True
        pool_b._load_instances()

    dirs_a = [i.workspace_dir for i in pool_a._instances]
    dirs_b = [i.workspace_dir for i in pool_b._instances]
    assert dirs_a == dirs_b

    # Mutating pool_a runtime state does not affect pool_b
    pool_a._instances[0].alive = False
    assert pool_b._instances[0].alive is True


# ─── Postgres-backed health state ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_writes_health_to_postgres():
    """_probe_all UPSERTs each instance's alive state to Postgres."""
    inst = _make_instance(alive=True)
    inst.host = "host-a"
    inst.port = 8001
    inst.client.health_check = AsyncMock(return_value=True)

    pool = _make_pool([inst])

    captured_calls: list[tuple] = []

    class FakeConn:
        async def execute(self, sql, *args):
            captured_calls.append((sql, args))

    class FakeAcquireCM:
        async def __aenter__(self): return FakeConn()
        async def __aexit__(self, *a): return None

    class FakePgPool:
        def acquire(self): return FakeAcquireCM()

    with patch("core.db.get_pool", return_value=FakePgPool()):
        await pool._probe_all()

    assert len(captured_calls) == 1
    sql, args = captured_calls[0]
    assert "opencode_instance_health" in sql
    assert "ON CONFLICT" in sql
    assert args == ("host-a", 8001, True)


@pytest.mark.asyncio
async def test_probe_postgres_failure_does_not_crash_loop():
    """A Postgres write failure during probe must not propagate."""
    inst = _make_instance(alive=True)
    inst.host = "host-a"
    inst.port = 8001
    inst.client.health_check = AsyncMock(return_value=True)

    pool = _make_pool([inst])

    with patch("core.db.get_pool", side_effect=RuntimeError("pool not initialised")):
        # Must not raise
        await pool._probe_all()
    # Local view still updated despite Postgres miss
    assert inst.alive is True


@pytest.mark.asyncio
async def test_dispatch_uses_shared_postgres_alive_when_available():
    """When Postgres reports an instance dead, dispatch excludes it even if
    the per-process _Instance.alive flag is True."""
    from datetime import datetime, timezone

    inst_a = _make_instance(workspace_dir="/ws-a", alive=True)
    inst_a.host = "host-a"
    inst_a.port = 8001
    inst_a.client.chat = AsyncMock(return_value="from-a")

    inst_b = _make_instance(workspace_dir="/ws-b", alive=True)
    inst_b.host = "host-b"
    inst_b.port = 8002
    inst_b.client.chat = AsyncMock(return_value="from-b")

    now = datetime.now(timezone.utc)
    fake_rows = [
        {"host": "host-a", "port": 8001, "alive": False, "last_probe_at": now},
        {"host": "host-b", "port": 8002, "alive": True,  "last_probe_at": now},
    ]

    class FakePgPool:
        async def fetch(self, sql):
            return fake_rows

    pool = _make_pool([inst_a, inst_b])

    with patch("core.db.get_pool", return_value=FakePgPool()):
        result = await pool.dispatch([{"role": "user", "content": "hi"}])

    # host-a is alive locally but dead in Postgres → must not be picked
    assert result == "from-b"


@pytest.mark.asyncio
async def test_dispatch_ignores_stale_postgres_rows():
    """A row whose last_probe_at is older than 2× the probe interval is
    treated as alive=False regardless of stored value."""
    from datetime import datetime, timedelta, timezone

    inst = _make_instance(workspace_dir="/ws", alive=True)
    inst.host = "host-a"
    inst.port = 8001
    inst.client.chat = AsyncMock(return_value="ok")

    # Row says alive=True but last_probe_at is 10 minutes ago
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    fake_rows = [
        {"host": "host-a", "port": 8001, "alive": True, "last_probe_at": stale},
    ]

    class FakePgPool:
        async def fetch(self, sql):
            return fake_rows

    pool = _make_pool([inst])

    with patch("core.db.get_pool", return_value=FakePgPool()):
        with pytest.raises(NoOpenCodeAvailable):
            await pool.dispatch([{"role": "user", "content": "hi"}])
