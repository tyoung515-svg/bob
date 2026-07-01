"""Shared pytest configuration for BoBClaw tests."""
import pytest


# Configure pytest-asyncio to automatically handle all async test functions
# without requiring explicit @pytest.mark.asyncio decorators.
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as asyncio"
    )
