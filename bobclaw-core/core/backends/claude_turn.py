"""CC2 continuation adapter: one CLI invocation, Bob's existing tool loop.

Each ainvoke releases the previous *server executed* tool result, then waits
for another bridge call or the final answer. It is an explicit CLI adapter,
not a LangChain provider/tool binding. Bob decides every dispatch and wait.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import re
import socket
import struct
import uuid

from langchain_core.messages import AIMessage
from core.backends.provider_failure import ProviderFailure, reset_time
from core.worker.contract import MAX_OUTPUT, WorkerUnavailable


class ClaudeFailure(ProviderFailure):
    @property
    def sentence(self):
        return 'Sign in to Claude again' if self.code == 'model_auth_rejected' else super().sentence


class ClaudeTurnError(RuntimeError):
    def __init__(self, failure=None, *, tools_ran=False):
        self.failure = failure or ProviderFailure()
        self.no_provider_retry = tools_ran
        super().__init__('Sign in to Claude again' if self.failure.code == 'model_auth_rejected' else self.failure.sentence)


def cli_reset(value):
    parsed = reset_time(value)
    if parsed or not isinstance(value, str) or len(value) > 64:
        return parsed
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if stamp.tzinfo else None
    except ValueError:
        return None


def failure(event):
    error = event.get('error')
    error = error if isinstance(error, dict) else {}
    code = error.get('code') or error.get('type') or (event.get('error') if isinstance(event.get('error'), str) else None) or event.get('subtype')
    status = error.get('status') or event.get('api_error_status')
    mapping = {'rate_limit_error': 'model_rate_limited', 'rate_limit_exceeded': 'model_rate_limited',
        'usage_limit_reached': 'model_usage_limit', 'context_length_exceeded': 'model_context_limit',
        'invalid_request_error': 'model_stopped', 'overloaded_error': 'model_provider_error',
        'api_error': 'model_provider_error', 'server_error': 'model_provider_error',
        'authentication_error': 'model_auth_rejected', 'not_authenticated': 'model_auth_rejected',
        'authentication_failed': 'model_auth_rejected', 'rate_limit': 'model_rate_limited',
        'error_max_turns': 'model_stopped', 'error_max_budget_usd': 'model_usage_limit',
        'error_during_execution': 'model_stopped'}
    category = mapping.get(code, 'model_rate_limited' if status == 429 else
        'model_auth_rejected' if status == 401 else 'model_provider_error' if isinstance(status, int) and status >= 500 else 'model_stopped')
    # Only narrow CLI phrases are recognized. Never log or expose raw prose.
    parts = [event.get('result'), error.get('message')]
    errors = event.get('errors')
    if isinstance(errors, list):
        parts.extend(errors[:8])
    message = event.get('message')
    if isinstance(message, dict) and isinstance(message.get('content'), list):
        parts.extend(b.get('text') for b in message['content'] if isinstance(b, dict) and b.get('type') == 'text')
    text = ' '.join(v[:4096] for v in parts if isinstance(v, str)).lower()
    if category == 'model_stopped':
        for marker, reason in [('not logged in', 'model_auth_rejected'), ('please run /login', 'model_auth_rejected'),
                ('usage limit', 'model_usage_limit'), ('hit your limit', 'model_usage_limit'),
                ('prompt is too long', 'model_context_limit'), ('context window', 'model_context_limit'),
                ('rate limit', 'model_rate_limited')]:
            if marker in text:
                category = reason
                break
    from core.backends.provider_failure import seconds, token
    return ClaudeFailure(category, 'claude.result', token(code), retry_after=seconds(error.get('retry_after')),
        resets_at=cli_reset(error.get('resets_at') or event.get('resets_at')), status=status if isinstance(status, int) else None)


def history_messages(messages):
    system, history = [], []
    for message in messages:
        role = getattr(message, 'type', None) or message.get('role')
        content = getattr(message, 'content', None) if not isinstance(message, dict) else message.get('content')
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if role == 'system':
            system.append(content)
        else:
            if role == 'tool':
                content = 'Bob tool result (evidence): ' + content
            calls = getattr(message, 'tool_calls', None)
            if calls:
                content += '\nBob assistant tool requests: ' + json.dumps(calls)
            history.append({'role': 'assistant' if role in {'ai', 'assistant'} else 'user', 'content': content})
    return '\n\n'.join(system), history


class ClaudeTurn:
    def __init__(self, *, model, tools=(), writer=None, connector=None, recorder=None):
        self.model = model
        self.tools = {tool.name: tool for tool in tools}
        self.writer = writer
        self.connector = connector
        if recorder is None:
            from .claude_state import record
            recorder = record
        self.recorder = recorder
        self.turn_id = uuid.uuid4().hex
        self.queue = asyncio.Queue()
        self.pending = None
        self.seen = set()
        self.task = None
        self.stream = None
        self.reader = None
        self.lease = None
        self.actual = None
        self.tools_ran = False
        self.closed = False
        self.result = None
        self.cleanup = None
        self.text = ''
        self.all_text = ''
        self.partial = False
        self.usage = None
        self.completed_result = None
        self.remaining_tools = []
        self.more_tools_in_round = False
        self.owner_task = None
        self.interrupted_tool_error = None
        self.declared_tool_ids = set()
        self.assistant_usage = {}
        self.usage_message_id = None
        self.result_usage_seen = False
        self.history_length = 0
        self.limit_reset = None
        self.quota_rejected = False

    def record(self, kind, payload):
        self.recorder(self.turn_id, kind, payload)

    def emit(self, event):
        if self.writer:
            self.writer(event)

    def visible(self, text):
        if text:
            self.text += text
            self.all_text += text
            self.emit({'type': 'token', 'content': text, 'backend': 'claude_code', 'model': self.actual or self.model})

    def observe(self, actual):
        if not isinstance(actual, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', actual):
            return
        if self.actual and actual != self.actual:
            from core.execution.models import ExecutionError
            raise ExecutionError('binding_mismatch', 409, 'The Claude-reported model changed within this turn.')
        if not self.actual:
            self.actual = actual
            self.record('model', {'selection': self.model, 'reported': actual})
            self.emit({'type': 'model_observation', 'backend': 'claude_code', 'model': self.model,
                       'provider_reported_model': actual, 'billing': 'subscription'})

    def record_usage(self, usage, *, source='result', partial=False):
        valid = isinstance(usage, dict) and any(k in usage for k in ('input_tokens', 'output_tokens'))
        values = {}
        if valid:
            for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
                value = usage.get(key, 0)
                if type(value) is not int or value < 0:
                    raise WorkerUnavailable('invalid Claude usage counters')
                values[key] = value
            incoming = sum(values[k] for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
            outgoing = values['output_tokens']
            self.usage = {'input_tokens': incoming, 'output_tokens': outgoing, 'total_tokens': incoming + outgoing}
        observation = {**(self.usage or {}), 'available': valid, 'partial': partial,
                       'source': source, 'billing': 'subscription', 'model': self.actual}
        self.record('usage', observation)
        self.emit({'type': 'model_usage', 'backend': 'claude_code', **observation})

    def tool_started(self, name, args, cid):
        self.tools_ran = True
        self.record('tool_dispatch', {'id': cid, 'name': name})

    def accept_result(self, cid, content):
        if not self.pending or cid != self.pending['id']:
            raise WorkerUnavailable('Claude tool correlation failed')
        self.record('tool_result', {'id': cid, 'name': self.pending['name'], 'content': content})
        self.completed_result = str(content)

    async def send(self, value):
        data = json.dumps(value).encode() + b'\n'
        if len(data) > MAX_OUTPUT:
            raise WorkerUnavailable('Claude response too large')
        self.stream.write(data)
        await self.stream.drain()

    async def start(self, messages):
        from core.lane_config import require_backend
        require_backend('claude_code', self.model)
        system, history = history_messages(messages)
        self.history_length = len(messages)
        schemas = [{'name': tool.name, 'description': tool.description or '',
                    'inputSchema': tool.get_input_schema().model_json_schema()} for tool in self.tools.values()]
        if self.connector:
            self.reader, self.stream = await self.connector()
            proxy = None
        else:
            from core.worker.client import configuration
            from core.worker.profile_network import ProfileNetwork
            cfg = configuration()
            self.lease = ProfileNetwork({'profile': 'claude', 'execution_id': self.turn_id, 'timeout': 86400}, cfg)
            await self.lease.__aenter__()
            proxy = self.lease.url
            self.reader, self.stream = await asyncio.open_unix_connection(cfg['claude_turn_socket'], limit=MAX_OUTPUT + 1)
            sock = self.stream.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise WorkerUnavailable('Claude listener identity mismatch')
        value = {'version': 1, 'model': self.model, 'system': system, 'history': history,
                 'tools': schemas, 'timeout': 86400, 'network_proxy': proxy}
        from core.worker.claude_turn_contract import validate_start
        validate_start(value)
        self.record('start', {'selection': self.model, 'tool_names': list(self.tools)})
        await self.send(value)
        self.task = asyncio.create_task(self.pump())

    async def rpc(self, event):
        cid, method = event.get('id'), event.get('method')
        if 'id' not in event:
            return  # MCP initialized/cancelled notifications carry no authority.
        if not isinstance(cid, (str, int)) or isinstance(cid, bool) or len(str(cid)) > 128 or cid in self.seen:
            await self.refuse(cid, 'invalid_or_repeated_request')
            return
        self.seen.add(cid)
        if self.closed or self.result is not None:
            await self.refuse(cid, 'turn_ended')
        elif method == 'initialize':
            await self.respond(cid, {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                                     'serverInfo': {'name': 'bob-turn', 'version': '1'}})
        elif method == 'ping':
            await self.respond(cid, {})
        elif method == 'tools/list':
            await self.respond(cid, {'tools': [{'name': t.name, 'description': t.description or '',
                'inputSchema': t.get_input_schema().model_json_schema()} for t in self.tools.values()]})
        elif method == 'tools/call':
            params = event.get('params')
            if not isinstance(params, dict) or set(params) - {'name', 'arguments', '_meta'}:
                await self.refuse(cid, 'invalid_tool_arguments')
                return
            name, args = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str) or not isinstance(args, dict) or self.pending is not None:
                await self.refuse(cid, 'tool_not_bound')
                return
            match = next((t for t in self.remaining_tools if t['name'] == name and t['args'] == args), None)
            if match:
                self.remaining_tools.remove(match)
            self.more_tools_in_round = bool(self.remaining_tools) if match else False
            if not self.actual:
                raise WorkerUnavailable('Claude did not report a model before requesting a tool')
            self.pending = {'rpc': cid, 'id': 'claude-' + uuid.uuid4().hex, 'name': name, 'args': args}
            self.record('tool_intent', self.pending)
            await self.queue.put(('call', self.pending))
        else:
            await self.refuse(cid, 'unknown_method')

    async def respond(self, cid, result):
        await self.send({'type': 'mcp', 'response': {'jsonrpc': '2.0', 'id': cid, 'result': result}})

    async def refuse(self, cid, reason):
        self.record('bridge_refused', {'request_id': cid, 'reason': reason})
        self.emit({'type': 'tool_activity', 'phase': 'result', 'tool': 'claude_bridge', 'ok': False, 'preview': reason})
        await self.send({'type': 'mcp', 'response': {'jsonrpc': '2.0', 'id': cid,
            'error': {'code': -32600, 'message': reason}}})

    async def pump(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line or len(line) > MAX_OUTPUT or not line.endswith(b'\n'):
                    raise ClaudeTurnError(tools_ran=self.tools_ran)
                value = json.loads(line)
                kind = value.get('type')
                if kind == 'mcp':
                    await self.rpc(value['event'])
                elif kind == 'cli':
                    event = value['event']
                    typ = event.get('type')
                    if typ == 'system' and event.get('subtype') == 'init':
                        advertised = event.get('tools')
                        expected = {'mcp__bob__' + name for name in self.tools}
                        if isinstance(advertised, list) and set(advertised) != expected:
                            raise WorkerUnavailable('Claude exposed a different tool surface')
                    elif typ == 'stream_event':
                        chunk = event.get('event', {})
                        if chunk.get('type') == 'message_start':
                            message = chunk.get('message', {})
                            self.observe(message.get('model'))
                            self.usage_message_id = message.get('id')
                            if self.usage_message_id and isinstance(message.get('usage'), dict):
                                self.assistant_usage[self.usage_message_id] = message['usage']
                        elif chunk.get('type') == 'message_delta' and self.usage_message_id and isinstance(chunk.get('usage'), dict):
                            self.assistant_usage.setdefault(self.usage_message_id, {}).update(chunk['usage'])
                        delta = chunk.get('delta') or {}
                        if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                            self.partial = True
                            self.visible(delta['text'])
                    elif typ == 'rate_limit_event':
                        info = event.get('rate_limit_info') or event.get('rateLimitInfo') or {}
                        if isinstance(info, dict):
                            self.limit_reset = cli_reset(info.get('resetsAt') or info.get('resets_at'))
                            self.quota_rejected = (info.get('status') == 'rejected' and
                                info.get('rateLimitType') in {'five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet'})
                    elif typ == 'assistant':
                        if event.get('error'):
                            problem = failure(event)
                            if self.quota_rejected and problem.code in {'model_rate_limited', 'model_stopped'}:
                                problem = replace(problem, code='model_usage_limit', resets_at=self.limit_reset)
                            raise ClaudeTurnError(problem, tools_ran=self.tools_ran)
                        message = event.get('message', {})
                        self.observe(message.get('model'))
                        if message.get('id') and isinstance(message.get('usage'), dict):
                            self.assistant_usage[message['id']] = message['usage']
                        for block in message.get('content', []):
                            if isinstance(block, dict) and block.get('type') == 'tool_use':
                                name = block.get('name', '')
                                if name.startswith('mcp__bob__'):
                                    name = name[len('mcp__bob__'):]
                                identity = block.get('id')
                                if not identity or identity not in self.declared_tool_ids:
                                    self.remaining_tools.append({'name': name, 'args': block.get('input', {})})
                                    if identity: self.declared_tool_ids.add(identity)
                        if not self.partial:
                            for block in message.get('content', []):
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    self.visible(block.get('text', ''))
                        self.partial = False
                    elif typ == 'result':
                        self.result_usage_seen = True
                        reported_turns = event.get('num_turns')
                        if type(reported_turns) is int and reported_turns >= 0:
                            self.record('reported_rounds', {'rounds': reported_turns, 'source': 'cli_result'})
                        if event.get('is_error') or str(event.get('subtype', '')).startswith('error'):
                            self.record_usage(event.get('usage'))
                            problem = failure(event)
                            if self.quota_rejected and problem.code in {'model_rate_limited', 'model_stopped'}:
                                problem = replace(problem, code='model_usage_limit')
                            if problem.code == 'model_usage_limit' and not problem.resets_at:
                                problem = replace(problem, resets_at=self.limit_reset)
                            if problem.code == 'model_auth_rejected':
                                self.record('signed_out', {})
                            raise ClaudeTurnError(problem, tools_ran=self.tools_ran)
                        # The assistant messages are the identity evidence; the result's
                        # usage table is only a fallback (it may list helper models).
                        models = event.get('modelUsage') or {}
                        for reported in ([] if self.actual else models):
                            self.observe(reported)
                        if not self.actual:
                            raise WorkerUnavailable('Claude did not report its model identity')
                        self.record_usage(event.get('usage'))
                        if not self.all_text and isinstance(event.get('result'), str):
                            self.visible(event['result'])
                        self.result = event
                    elif typ == 'error':
                        raise ClaudeTurnError(failure(event), tools_ran=self.tools_ran)
                    from core.execution.workflow_context import current
                    workflow = current.get()
                    if workflow:
                        await workflow.flush()
                elif kind == 'terminal':
                    self.cleanup = value.get('cleanup')
                    if value.get('cleanup') != 'idle_proved' or value.get('returncode') or not self.result:
                        raise ClaudeTurnError(tools_ran=self.tools_ran)
                    await self.queue.put(('result', None))
                    return
                elif kind == 'error':
                    raise ClaudeTurnError(tools_ran=self.tools_ran)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(exc, ClaudeTurnError):
                from core.execution.models import ExecutionError
                if not isinstance(exc, ExecutionError):
                    exc = ClaudeTurnError(tools_ran=self.tools_ran)
            if isinstance(exc, ClaudeTurnError):
                exc.no_provider_retry = self.tools_ran or bool(self.all_text)
                try:
                    if exc.failure.code == 'model_auth_rejected':
                        self.record('signed_out', {})
                    self.record('failure', {'code': exc.failure.code})
                except Exception:
                    exc.no_provider_retry = True
            await self.queue.put(('error', exc))
            if (self.pending and self.tools_ran and self.completed_result is None and not self.closed
                    and self.owner_task and not self.owner_task.done() and not self.owner_task.cancelling()):
                # Revoke an outstanding approval/tool await immediately on CLI
                # death. Its usual cancellation path reconciles any worker job.
                self.interrupted_tool_error = exc
                self.owner_task.cancel()

    async def ainvoke(self, messages):
        self.owner_task = asyncio.current_task()
        if self.closed:
            raise ClaudeTurnError(tools_ran=self.tools_ran)
        if self.task is None:
            await self.start(messages)
        if self.task.done() and not self.queue.empty():
            kind, value = await self.queue.get()
            if kind == 'error':
                raise value
            await self.queue.put((kind, value))
        updates = [m.content for m in messages[self.history_length:] if getattr(m, 'type', None) == 'human']
        self.history_length = len(messages)
        if self.pending:
            result = next((m for m in reversed(messages) if getattr(m, 'type', None) == 'tool'
                           and m.tool_call_id == self.pending['id']), None)
            if result is None:
                raise WorkerUnavailable('Claude tool result missing')
            if self.completed_result is None:
                self.accept_result(self.pending['id'], result.content)
            self.tools_ran = True
            cid = self.pending['rpc']
            content = self.completed_result
            if updates:
                self.record('steering', {'messages': updates})
                content += '\nServer-delivered owner messages, subject to the unchanged grant: ' + json.dumps(updates)
            self.pending = None
            self.completed_result = None
            await self.respond(cid, {'content': [{'type': 'text', 'text': content}]})
        kind, value = await self.queue.get()
        if kind == 'error':
            if isinstance(value, ClaudeTurnError) and not value.no_provider_retry:
                await self.close()
                if self.cleanup != 'idle_proved':
                    value.no_provider_retry = True
                    raise value
                self.closed = False
                self.task = None
                self.queue = asyncio.Queue()
                self.pending = None
                self.seen = set()
                self.actual = None
                self.text = self.all_text = ''
                self.result = None
                self.cleanup = None
                self.partial = False
                self.remaining_tools = []
                self.more_tools_in_round = False
                self.declared_tool_ids = set()
                self.assistant_usage = {}
                self.usage_message_id = None
                self.result_usage_seen = False
                self.limit_reset = None
                self.quota_rejected = False
                self.usage = None
            raise value
        text, self.text = self.text, ''
        return AIMessage(content=text, tool_calls=[{'name': value['name'], 'args': value['args'], 'id': value['id']}] if kind == 'call' else [],
            response_metadata={'model_name': self.actual, 'bob_streamed': True}, usage_metadata=self.usage)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.stream:
                try:
                    await self.send({'type': 'stop'})
                    async with asyncio.timeout(7):
                        if self.task and not self.task.done():
                            await asyncio.shield(self.task)
                        while self.cleanup is None:
                            line = await self.reader.readline()
                            if not line:
                                break
                            value = json.loads(line)
                            if value.get('type') == 'terminal':
                                self.cleanup = value.get('cleanup')
                            elif value.get('type') == 'mcp':
                                await self.rpc(value['event'])
                except (OSError, TimeoutError, RuntimeError, ValueError):
                    pass
                self.stream.close()
                try:
                    async with asyncio.timeout(2):
                        await self.stream.wait_closed()
                except (OSError, TimeoutError):
                    pass
        finally:
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if self.lease:
                await self.lease.__aexit__(None, None, None)
                self.lease = None
            if not self.result_usage_seen:
                aggregate = {}
                for usage in self.assistant_usage.values():
                    for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
                        value = usage.get(key, 0)
                        if type(value) is int and value >= 0:
                            aggregate[key] = aggregate.get(key, 0) + value
                self.record_usage(aggregate or None, source='assistant_events', partial=True)
            self.record('ended', {'bridge_requests': len(self.seen), 'had_tool': self.tools_ran,
                                 'cleanup': self.cleanup or 'unknown', 'status': 'completed' if self.result else 'interrupted'})


async def text_stream(messages, model=None):
    """Tool-free stateless internal transport, still subscription-only."""
    from core.lane_config import get_lane
    deltas = asyncio.Queue()
    def writer(event):
        if event.get('type') == 'token':
            deltas.put_nowait(event['content'])
    turn = ClaudeTurn(model=model or get_lane().backends['claude_code'].model, writer=writer)
    async def run():
        try:
            from core.nodes.execute import _dict_messages_to_lc
            response = await turn.ainvoke(_dict_messages_to_lc(messages))
            if response.tool_calls:
                raise WorkerUnavailable('Claude requested a tool on a text-only turn')
        except BaseException as exc:
            await deltas.put(exc)
        finally:
            try:
                await turn.close()
            except BaseException as exc:
                await deltas.put(exc)
            finally:
                await deltas.put(None)
    task = asyncio.create_task(run())
    try:
        while True:
            delta = await deltas.get()
            if delta is None:
                return
            if isinstance(delta, BaseException):
                raise delta
            yield delta
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
