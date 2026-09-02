"""
Shared fixtures for bobclaw-core test suite.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

# Test baselines must win over the repo .secrets file: core.config loads
# .secrets/bobclaw.env with override=False at import, so anything set here
# first is immune to local env drift (e.g. a real KIMI_MODEL override breaking
# the code-default guard in test_kimi_backend).
os.environ.setdefault("KIMI_MODEL", "kimi-k2.7-code")


@pytest.fixture(autouse=True)
def mock_redis():
    """Patch _get_redis in core.nodes.execute with an AsyncMock (autouse).

    The returned mock client has:
    - ``get`` returning ``None`` (no pin by default)
    - ``set`` returning ``True``
    Tests can override ``mock_redis.get.return_value`` etc.
    """
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(return_value=True)
    with patch("core.nodes.execute._get_redis", return_value=client):
        yield client


def pytest_collection_modifyitems(config, items):
    """Skip ``@pytest.mark.integration`` tests unless ``-m integration`` is passed.

    This keeps the default ``pytest -q`` run free of integration tests
    (which require a running Qdrant container and seeded fixtures).
    """
    if config.getoption("-m") == "integration":
        return
    skip_integration = pytest.mark.skip(reason="integration test (use pytest -m integration)")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_integration)
