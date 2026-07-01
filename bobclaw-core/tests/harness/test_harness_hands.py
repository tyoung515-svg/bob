import pytest
from core.harness.hands import BackendHands, RegistryHands
from core.harness.interfaces import RetryableToolCallError, FatalToolCallError


@pytest.mark.asyncio
async def test_backend_hands_execute_invokes_sender_and_returns_reply():
    """BackendHands.execute calls the injected sender with correct arguments and returns its reply."""
    called = {"messages": None, "backend": None}

    async def fake_sender(messages, backend):
        called["messages"] = messages
        called["backend"] = backend
        return "REPLY"

    hands = BackendHands(sender=fake_sender)
    result = await hands.execute("deepseek_v4_flash", "hi")
    assert result == "REPLY"
    assert called["messages"] == [{"role": "user", "content": "hi"}]
    assert called["backend"] == "deepseek_v4_flash"


@pytest.mark.asyncio
async def test_backend_hands_generic_error_wraps_as_retryable():
    """A sender that raises a generic Exception yields RetryableToolCallError."""

    async def failing_sender(messages, backend):
        raise RuntimeError("backend is down")

    hands = BackendHands(sender=failing_sender)
    with pytest.raises(RetryableToolCallError) as excinfo:
        await hands.execute("some_backend", "test")
    assert excinfo.value.retryable is True
    # Ensure the original error message is preserved
    assert "backend is down" in str(excinfo.value)


@pytest.mark.asyncio
async def test_backend_hands_fatal_error_passes_through():
    """A sender that raises FatalToolCallError is NOT wrapped."""

    async def fatal_sender(messages, backend):
        raise FatalToolCallError("permanent failure")

    hands = BackendHands(sender=fatal_sender)
    with pytest.raises(FatalToolCallError) as excinfo:
        await hands.execute("some_backend", "test")
    assert excinfo.value.retryable is False
    assert "permanent failure" in str(excinfo.value)


@pytest.mark.asyncio
async def test_registry_hands_known_name_dispatches():
    """RegistryHands.execute dispatches a known name to its registered callable and returns the result."""

    async def echo(input_str: str) -> str:
        return f"echoed: {input_str}"

    registry = {"echo": echo}
    hands = RegistryHands(registry)
    result = await hands.execute("echo", "hello")
    assert result == "echoed: hello"


@pytest.mark.asyncio
async def test_registry_hands_unknown_name_raises_fatal():
    """An unknown hand name raises FatalToolCallError."""

    async def dummy(input_str: str) -> str:
        return "never called"

    registry = {"exists": dummy}
    hands = RegistryHands(registry)
    with pytest.raises(FatalToolCallError) as excinfo:
        await hands.execute("nonexistent", "input")
    assert excinfo.value.retryable is False
    assert "nonexistent" in str(excinfo.value)


@pytest.mark.asyncio
async def test_registry_hands_generic_error_wraps_as_retryable():
    """A registered callable that raises a generic Exception yields RetryableToolCallError."""

    async def broken(input_str: str) -> str:
        raise ValueError("internal failure")

    registry = {"broken": broken}
    hands = RegistryHands(registry)
    with pytest.raises(RetryableToolCallError) as excinfo:
        await hands.execute("broken", "anything")
    assert excinfo.value.retryable is True
    assert "internal failure" in str(excinfo.value)
