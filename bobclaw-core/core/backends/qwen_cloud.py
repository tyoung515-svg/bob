"""Qwen cloud OpenAI-compatible transport; separate from local Qwen research."""
import aiohttp

from core.config import config
from core.backends.kimi import KimiClient


class QwenCloudClient(KimiClient):
    """Reuse the bounded HTTP/SSE transport, without Kimi membership parameters."""

    def __init__(self, base_url=None, api_key=None):
        self.base_url = (base_url or config.QWEN_BASE_URL).rstrip('/')
        self.api_key = api_key or config.DASHSCOPE_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=180, connect=5)
        self._discover_timeout = aiohttp.ClientTimeout(total=5)

    @property
    def api_key(self):
        from core.settings_client import provider_key
        return provider_key('qwen', self._api_key)

    @api_key.setter
    def api_key(self, value):
        self._api_key = value

    @staticmethod
    def _prepare_payload(*, model, messages, stream, **kwargs):
        return {'model': model or config.QWEN_MODEL, 'messages': messages,
                'stream': stream, **{k: v for k, v in kwargs.items() if v is not None}}
