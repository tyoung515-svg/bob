"""Exact text transports: one charged dispatch, no tools/retries/fallback."""
from __future__ import annotations

import aiohttp

from .models import ExecutionError

# These transports have an explicitly single-request adapter below. CLI and PC
# seats stay visible but ineligible; registration alone grants no run authority.
COMPATIBLE = {
    "deepseek_v4_flash": ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"),
    "deepseek_v4": ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY"),
    "glm_5_3": ("ZAI_BASE_URL", "ZAI_API_KEY"),
    "kimi_code": ("KIMI_BASE_URL", "KIMI_API_KEY"),
    "qwen_cloud": ("QWEN_BASE_URL", "DASHSCOPE_API_KEY"),
    "kimi_platform": ("KIMI_PLATFORM_BASE_URL", "MOONSHOT_API_KEY"),
    "minimax": ("MINIMAX_BASE_URL", "MINIMAX_API_KEY"),
}
EXACT_BACKENDS = set(COMPATIBLE) | {"openai_codex", "claude_api"}


def exact_models(models):
    return {key: {**spec, "exact_calls": spec.get("transport") == "text" and spec["backend"] in EXACT_BACKENDS}
            for key, spec in models.items()}


class Dispatcher:
    async def healthy(self, binding):
        """Non-generating readiness only; never probe an unapproved transport.

        Use the existing health client registry, but fail closed on exceptions or
        timeout rather than inheriting the legacy resolver's fail-open policy.
        A readiness result cannot promise a subsequent inference will succeed.
        """
        import asyncio
        try:
            from core.lane_config import configured_mode, require_backend
            backend = binding["backend"]
            if backend not in EXACT_BACKENDS or not binding.get("exact_calls") or binding.get("transport") != "text":
                return False
            if configured_mode():
                require_backend(backend, binding["model"])
            from core.health_probe import _make_client
            async with asyncio.timeout(6):
                return await _make_client(backend).health_check() is True
        except Exception:
            return False

    async def __call__(self, binding, prompt):
        from .call_guard import consume_permit
        consume_permit()
        from core.config import config
        from core.lane_config import configured_mode, require_backend
        backend, model = binding["backend"], binding["model"]
        if backend not in EXACT_BACKENDS or not binding.get("exact_calls"):
            raise ExecutionError("opaque_call_accounting", 409)
        if configured_mode():
            require_backend(backend, model)
        messages = [{"role": "system", "content": "Read-only supplied-evidence team. No tools or execution authority. Preserve source IDs; distinguish evidence from inference."},
                    {"role": "user", "content": prompt}]
        if backend == "openai_codex":
            from core.backends.openai_codex import OpenAICodexClient, CodexProviderError
            try:
                result = await OpenAICodexClient().complete(messages, model=model, retry_unauthorized=False)
            except CodexProviderError as exc:
                if exc.code in {"provider_error", "provider_timeout", "invalid_provider_response"}:
                    raise ExecutionError(exc.code, 502, {"status": exc.status, "retry_after": exc.retry_after}) from None
                raise  # credential/policy errors never become transient failures
            if result.get("tool_calls"):
                raise ExecutionError("unexpected_tool_call", 502)
            return {"text": result["content"], "usage": result.get("usage", {})}
        if backend == "claude_api":
            from core.backends.claude import ClaudeClient
            # Claude's non-streaming client makes exactly one HTTP POST.
            try:
                result = await ClaudeClient().chat(messages[1:], model=model)
            except (ValueError, aiohttp.ContentTypeError):
                raise ExecutionError("invalid_provider_response", 502) from None
            if any(c.get("type") == "tool_use" for c in result.get("content", [])):
                raise ExecutionError("unexpected_tool_call", 502)
            return {"text": "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"),
                    "usage": result.get("usage", {})}
        url_attr, key_attr = COMPATIBLE[backend]
        key = getattr(config, key_attr)
        provider = {'deepseek_v4': 'deepseek', 'deepseek_v4_flash': 'deepseek',
                    'glm_5_3': 'glm', 'kimi_code': 'kimi', 'qwen_cloud': 'qwen'}.get(backend)
        if provider:
            from core.settings_client import provider_key
            key = provider_key(provider, key)
        url = getattr(config, url_attr).rstrip("/") + "/chat/completions"
        payload = {"model": model, "messages": messages, "stream": False}
        if backend.startswith("deepseek"):
            payload["thinking"] = {"type": "disabled"}
        from .call_guard import consume_transport
        consume_transport()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180, connect=5)) as session:
            async with session.post(url, headers={"Authorization": "Bearer " + key},
                                    json=payload, allow_redirects=False) as response:
                if response.status != 200:
                    raise ExecutionError("provider_error", 502, {"status": response.status,
                                                               "retry_after": response.headers.get("Retry-After", "")[:128]})
                # Bound provider data before decoding/persisting it.
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ExecutionError("provider_output_too_large", 502)
                import json
                try:
                    result = json.loads(raw)
                    message = result["choices"][0]["message"]
                    if not isinstance(message, dict):
                        raise ValueError()
                except (ValueError, KeyError, TypeError, IndexError):
                    raise ExecutionError("invalid_provider_response", 502) from None
        if message.get("tool_calls"):
            raise ExecutionError("unexpected_tool_call", 502)
        return {"text": message.get("content"), "usage": result.get("usage", {})}
