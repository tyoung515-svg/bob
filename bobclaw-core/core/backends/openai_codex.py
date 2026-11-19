"""ChatGPT sign-in Responses transport and LangChain tool adapter.

Responses conversion, identity headers and catalog ranking adapted from Hermes
(MIT, Nous Research). Bob owns the lane authority, transport and error contract.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from core.lane_config import get_lane
from core.backends.openai_codex_models import visible_model_slugs
from core.backends.openai_codex_auth import LaneCodexAuth, CodexProviderError, jwt_claims

BASE_URL = "https://chatgpt.com/backend-api/codex"
# Client functions must not collide with server-executed Responses tools.
RESERVED_NAMES = frozenset({"web_search", "web_search_preview", "file_search", "search_files", "computer", "computer_use_preview", "code_interpreter", "image_generation", "tool_search"})
_MODEL_CACHE: dict[tuple[str, str], str] = {}


def request_headers(credentials) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {credentials.access}",
               "originator": "bob", "User-Agent": "Bob/1.0",
               "Accept": "text/event-stream", "ChatGPT-Account-ID": credentials.account}
    claims = jwt_claims(credentials.access).get("https://api.openai.com/auth") or {}
    if isinstance(claims, dict):
        residency = claims.get("chatgpt_data_residency") or claims.get("chatgpt_compute_residency")
        if isinstance(residency, str) and residency.strip():
            headers["x-openai-internal-codex-residency"] = residency.strip()
    return headers


def wire_tools(tools: list[dict]) -> tuple[list[dict], dict[str, str]]:
    functions = [dict(t.get("function", t)) for t in tools]
    taken = {t["name"] for t in functions}
    aliases = {}
    result = []
    for tool in functions:
        name = tool["name"]
        if name in RESERVED_NAMES:
            base = alias = "bob_" + name
            suffix = 2
            while alias in taken:
                alias, suffix = f"{base}_{suffix}", suffix + 1
            taken.add(alias)
            aliases[alias] = name
            tool["name"] = alias
        result.append({**tool, "type": "function"})
    return result, aliases


def responses_input(messages: list[dict], aliases: dict[str, str]) -> tuple[str, list[dict]]:
    instructions, items = [], []
    names = {original: alias for alias, original in aliases.items()}
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise CodexProviderError("only text conversation input is supported")
        if role in {"system", "developer"}:
            instructions.append(content)
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content})
        elif role in {"user", "assistant"}:
            items.extend(message.get("codex_reasoning") or [])
            if content:
                items.append({"role": role, "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": content}]})
            for call in message.get("tool_calls") or []:
                function = call.get("function", call)
                name = function["name"]
                arguments = function.get("arguments", function.get("args", {}))
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": names.get(name, name),
                              "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)})
        else:
            raise CodexProviderError("unsupported conversation role")
    return "\n\n".join(instructions), items


def provider_failure(payload=None, *, event_type=None, status=None, headers=None, fallback=None):
    from core.backends.provider_failure import from_payload
    failure = from_payload(payload, event_type=event_type, status=status, headers=headers, fallback=fallback)
    failure.log()
    return CodexProviderError(failure.sentence, status, code=failure.code,
                              retry_after=failure.retry_after, failure=failure)


def catalog_slugs(catalog):
    try:
        return visible_model_slugs(catalog)
    except CodexProviderError as exc:
        failure = provider_failure(event_type='catalog.invalid')
        # The catalog validator has fixed, content-free diagnostics; preserve
        # them for setup callers while giving chat the same semantic contract.
        exc.failure, exc.code = failure.failure, failure.code
        exc.event_type = failure.event_type
        raise


async def parse_sse(response: httpx.Response, aliases: dict[str, str]) -> dict:
    """Collect delta/item streams, accepting only a completed terminal response."""
    text_parts, items, data = [], {}, []
    completed = None

    def consume(raw):
        nonlocal completed
        if raw == "[DONE]":
            return
        event, kind = None, None
        try:
            event = json.loads(raw)
            kind = event.get("type", "")
            if not isinstance(kind, str):
                raise ValueError()
            if kind in {'response.output_text.delta', 'response.function_call_arguments.delta'} and not isinstance(event.get('delta'), str):
                raise ValueError()
            if kind in {'response.output_item.added', 'response.output_item.done',
                        'response.function_call_arguments.delta', 'response.function_call_arguments.done'}:
                if type(event.get('output_index')) is not int or event['output_index'] < 0:
                    raise ValueError()
            if kind in {'response.output_item.added', 'response.output_item.done'} and not isinstance(event.get('item'), dict):
                raise ValueError()
            if kind == 'response.function_call_arguments.done' and not isinstance(event.get('arguments'), str):
                raise ValueError()
            if kind in {"error", "response.failed", "response.incomplete", "response.cancelled"}:
                raise provider_failure(event, event_type=kind, headers=response.headers)
            if kind == "response.output_text.delta":
                text_parts.append(event["delta"])
            elif kind in {"response.output_item.added", "response.output_item.done"}:
                items[event["output_index"]] = event["item"]
            elif kind == "response.function_call_arguments.delta":
                item = items.setdefault(event["output_index"], {"type": "function_call"})
                item["arguments"] = item.get("arguments", "") + event["delta"]
            elif kind == "response.function_call_arguments.done":
                items.setdefault(event["output_index"], {})["arguments"] = event["arguments"]
            elif kind == "response.completed":
                completed = event["response"]
                if not isinstance(completed, dict):
                    raise ValueError()
                if completed.get('status', 'completed') != 'completed':
                    raise provider_failure(event, event_type=kind, headers=response.headers)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise provider_failure(event, event_type=kind or "stream.malformed") from None

    async for line in response.aiter_lines():
        if not line:
            if data:
                consume("\n".join(data))
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        consume("\n".join(data))
    if not isinstance(completed, dict) or completed.get("status", "completed") != "completed":
        raise provider_failure({'response': completed} if isinstance(completed, dict) else None,
                               event_type='stream.ended_without_completion', headers=response.headers,
                               fallback='model_provider_error')
    text, calls, reasoning = [], [], []
    try:
        output = completed.get("output") or [items[k] for k in sorted(items)]
        if not isinstance(output, list):
            raise ValueError()
        for item in output:
            if item.get("type") == "message":
                text.extend(c["text"] for c in item.get("content", []) if c.get("type") == "output_text")
            elif item.get("type") == "function_call":
                name = item["name"]
                arguments = json.loads(item.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError()
                calls.append({"id": item["call_id"], "name": aliases.get(name, name), "args": arguments, "type": "tool_call"})
            elif item.get("type") == "reasoning":
                # Replay encrypted state only; summaries are never answer text.
                if item.get("encrypted_content"):
                    reasoning.append({k: item[k] for k in ("type", "id", "encrypted_content", "summary") if k in item})
        visible_content = "".join(text) if text else "".join(text_parts)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise provider_failure(event_type="stream.invalid_output") from None
    return {"content": visible_content,
            "tool_calls": calls, "reasoning": reasoning, "usage": completed.get("usage") or {},
            "model": completed.get("model") if isinstance(completed.get("model"), str) else None}


class OpenAICodexClient:
    def __init__(self, *, lane=None, transport=None, clock=None, lock_timeout=30):
        self.lane = lane if lane is not None else get_lane()
        self.auth = LaneCodexAuth(self.lane, **({"clock": clock} if clock else {}), lock_timeout=lock_timeout)
        self.transport = transport

    def __repr__(self):
        return "OpenAICodexClient(<lane credentials redacted>)"

    async def _request(self, client, method, path, credentials, budget, *, payload=None, aliases=None):
        for _ in range(2):
            headers = request_headers(credentials)
            if method == "GET":
                headers["Accept"] = "application/json"
            else:
                from core.execution.call_guard import consume_transport
                consume_transport()
            async with client.stream(method, BASE_URL + path, headers=headers, json=payload) as response:
                status = response.status_code
                if status == 401 and budget[0]:
                    budget[0] = False
                elif status >= 400:
                    await response.aread()
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    error = provider_failure(body, event_type='http.error', status=status,
                                             headers=response.headers)
                    if status == 401:
                        # Preserve the existing sign-in diagnostic for direct callers.
                        error.args = ('openai_codex: ChatGPT session rejected; sign in again',)
                    raise error
                elif method == "GET":
                    await response.aread()
                    try:
                        return response.json(), credentials
                    except ValueError:
                        raise provider_failure(event_type="catalog.malformed") from None
                else:
                    return await parse_sse(response, aliases or {}), credentials
            credentials = await self.auth.credentials(client, previous=credentials)
        raise CodexProviderError("ChatGPT session rejected; sign in again", 401)

    async def visible_models(self):
        """Read-only account catalog: no generation, refresh, auth or lane writes."""
        self.lane.require("openai_codex")
        try:
            async with asyncio.timeout(20), httpx.AsyncClient(
                    transport=self.transport, timeout=httpx.Timeout(20, connect=5), trust_env=False) as client:
                credentials = self.auth.load()
                catalog, _ = await self._request(client, "GET", "/models?client_version=1.0.0",
                                                  credentials, [False])
                return catalog_slugs(catalog)
        except (httpx.HTTPError, TimeoutError):
            raise provider_failure(event_type="catalog.transport_failed", fallback="model_provider_error") from None

    async def _model(self, client, credentials, budget):
        row = self.lane.backends.get("openai_codex")
        if row is None or not row.available:
            raise CodexProviderError("ChatGPT sign-in is not configured in this lane")
        if row.model:
            return row.model, credentials
        key = (str(self.lane.root), credentials.account)
        if key not in _MODEL_CACHE:
            catalog, credentials = await self._request(client, "GET", "/models?client_version=1.0.0", credentials, budget)
            _MODEL_CACHE[key] = catalog_slugs(catalog)[0]
        return _MODEL_CACHE[key], credentials

    async def complete(self, messages: list[dict], *, tools=None, model=None, retry_unauthorized=True) -> dict:
        from core.execution.call_guard import assert_transport_permitted, required
        assert_transport_permitted()
        if required.get():
            retry_unauthorized = False
        # Admit the exact lane binding before any refresh or provider request.
        self.lane.require("openai_codex", model)
        try:
            async with asyncio.timeout(180), httpx.AsyncClient(
                    transport=self.transport, timeout=httpx.Timeout(180, connect=5), trust_env=False) as client:
                credentials = await self.auth.credentials(client)
                budget = [retry_unauthorized]
                if model:
                    selected = model
                else:
                    selected, credentials = await self._model(client, credentials, budget)
                definitions, aliases = wire_tools(tools or [])
                instructions, items = responses_input(messages, aliases)
                payload = {"model": model or selected, "instructions": instructions, "input": items,
                           "store": False, "stream": True, "include": ["reasoning.encrypted_content"]}
                if definitions:
                    payload.update(tools=definitions, tool_choice="auto", parallel_tool_calls=True)
                result, _ = await self._request(client, "POST", "/responses", credentials, budget,
                                                payload=payload, aliases=aliases)
                # Preserve only the provider-observed name for BU5 validation.
                result["requested_model"] = model or selected
                return result
        except (httpx.HTTPError, TimeoutError):
            raise provider_failure(event_type="transport.failed", fallback="model_provider_error") from None

    async def chat(self, messages: list[dict], model=None, **kwargs) -> dict:
        result = await self.complete(messages, model=model, tools=kwargs.get("tools"))
        calls = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                 for c in result["tool_calls"]]
        return {"model": result["model"], "choices": [{"message": {"role": "assistant", "content": result["content"], "tool_calls": calls}}],
                "usage": result["usage"]}

    async def stream_chat(self, messages: list[dict], model=None, **kwargs):
        # The wire is SSE; publish only the verified final answer (no reasoning).
        result = await self.complete(messages, model=model, retry_unauthorized=False)
        writer = kwargs.get('writer')
        if writer:
            writer({'type': 'model_observation', 'backend': 'openai_codex',
                'model': result['requested_model'], 'provider_reported_model': result['model'],
                'billing': 'subscription'})
            writer({'type': 'model_usage', 'backend': 'openai_codex', 'model': result['model'],
                'billing': 'subscription', 'available': bool(result['usage']),
                **{key: result['usage'].get(key, 0) for key in ('input_tokens', 'output_tokens')},
                'total_tokens': result['usage'].get('total_tokens',
                    result['usage'].get('input_tokens', 0) + result['usage'].get('output_tokens', 0))})
        if result["content"]:
            yield result["content"]

    async def health_check(self) -> bool:
        # Configuration evidence only; no refresh or generation during probes.
        try:
            self.lane.require("openai_codex")
            self.auth.load()
            return True
        except (ValueError, CodexProviderError):
            return False


class ChatOpenAICodex(BaseChatModel):
    client: Any = Field(default_factory=OpenAICodexClient, exclude=True, repr=False)
    model_override: str | None = None

    @property
    def _llm_type(self) -> str:
        return "openai_codex"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("openai_codex uses async generation")

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        converted = []
        roles = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
        for message in messages:
            row = {"role": roles.get(message.type, message.type), "content": message.content}
            if message.type == "tool":
                row["tool_call_id"] = message.tool_call_id
            if isinstance(message, AIMessage):
                row.update(tool_calls=message.tool_calls, codex_reasoning=message.additional_kwargs.get("codex_reasoning"))
            converted.append(row)
        result = await self.client.complete(converted, tools=kwargs.get("tools"), model=self.model_override,
                                            retry_unauthorized=False)
        answer = AIMessage(content=result["content"], tool_calls=result["tool_calls"],
                           additional_kwargs={"codex_reasoning": result["reasoning"]},
                           response_metadata={"model_name": result["model"],
                                              "requested_model": result['requested_model']},
                           usage_metadata={"input_tokens": result['usage'].get('input_tokens', 0),
                                           "output_tokens": result['usage'].get('output_tokens', 0),
                                           "total_tokens": result['usage'].get('total_tokens',
                                               result['usage'].get('input_tokens', 0) + result['usage'].get('output_tokens', 0))}
                           if any(k in result['usage'] for k in ('input_tokens', 'output_tokens')) else None)
        return ChatResult(generations=[ChatGeneration(message=answer)])
