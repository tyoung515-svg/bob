from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.harness.interfaces import (
    FatalToolCallError,
    RetryableToolCallError,
    WorkerError,
)


class BackendHands:
    """Default hands over the existing backend dispatch.

    Uses _send_to_backend from core.nodes.execute unless an injectable
    sender is provided (for pure tests).
    """

    def __init__(self, sender: Callable[..., Awaitable[str]] | None = None) -> None:
        """Store optional injectable sender.

        Args:
            sender: An async callable (messages, backend) -> str.
                    When None, _send_to_backend is lazily imported inside execute().
        """
        self._sender = sender

    async def execute(self, name: str, input: str) -> str:
        """Execute a hand by name with the given input.

        Args:
            name: Backend identifier (e.g. "deepseek_v4_flash").
            input: Prompt/payload string.

        Returns:
            Reply text from the backend.

        Raises:
            WorkerError: Passed through unchanged.
            RetryableToolCallError: Wraps any other exception.
        """
        try:
            if self._sender is not None:
                send = self._sender
            else:
                # Lazy import to avoid dragging in network transport at module level.
                from core.nodes.execute import _send_to_backend as send
            return str(await send([{"role": "user", "content": input}], name))
        except WorkerError:
            raise
        except Exception as exc:
            raise RetryableToolCallError(
                str(exc),
                name=name,
                input=input,
            ) from exc


class RegistryHands:
    """Generic hands backed by a name → async callable registry."""

    def __init__(self, registry: dict[str, Callable[[str], Awaitable[str]]]) -> None:
        """Store the registry mapping hand names to async functions.

        Args:
            registry: Dict of name -> async callable(input) -> str.
        """
        self._registry = registry

    async def execute(self, name: str, input: str) -> str:
        """Execute a hand by name from the registry.

        Args:
            name: Hand name (key in registry).
            input: Input string passed to the registered callable.

        Returns:
            Result of the callable (cast to str).

        Raises:
            FatalToolCallError: If name is not in registry.
            WorkerError: Passed through unchanged.
            RetryableToolCallError: Wraps any other exception from the callable.
        """
        fn = self._registry.get(name)
        if fn is None:
            raise FatalToolCallError(
                f"unknown hand: {name}",
                name=name,
                input=input,
            )

        try:
            result = await fn(input)
            return str(result)
        except WorkerError:
            raise
        except Exception as exc:
            raise RetryableToolCallError(
                str(exc),
                name=name,
                input=input,
            ) from exc
