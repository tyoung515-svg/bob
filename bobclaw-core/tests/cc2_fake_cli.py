"""Scripted stream-json/MCP CLI, for CC2 tests only; no model/provider calls."""
import asyncio
import json
import sys

MODEL = 'claude-sonnet-test-full'


async def script(operations, emit, rpc, *, model=MODEL):
    await rpc('initialize', {'protocolVersion': '2024-11-05'})
    await rpc('tools/list', {})
    full = ''
    for step, op in enumerate(operations):
        if 'text' in op:
            text = op['text']
            await emit({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'model': model}}})
            await emit({'type': 'stream_event', 'event': {'type': 'content_block_delta',
                'delta': {'type': 'thinking_delta', 'thinking': 'PRIVATE THINKING'}}})
            for word in text:
                await emit({'type': 'stream_event', 'event': {'type': 'content_block_delta',
                    'delta': {'type': 'text_delta', 'text': word}}})
            await emit({'type': 'assistant', 'message': {'model': model, 'content': [{'type': 'text', 'text': text}]}})
            full += text
        if 'tools' in op:
            await emit({'type': 'assistant', 'message': {'model': model, 'content': [
                {'type': 'tool_use', 'id': 'tool-'+str(step)+'-'+str(i), 'name': 'mcp__bob__'+t['name'], 'input': t.get('args', {})}
                for i, t in enumerate(op['tools'])]}})
            for tool in op['tools']:
                await rpc('tools/call', {'name': tool['name'], 'arguments': tool.get('args', {})})
        if 'raw' in op:
            await emit(op['raw'])
        if 'wait' in op:
            await asyncio.sleep(op['wait'])
        if 'failure' in op:
            await emit({'type': 'result', 'is_error': True, **op['failure']})
            return
        if 'model' in op:
            await emit({'type': 'assistant', 'message': {'model': op['model'], 'content': []}})
        if op.get('exit'):
            return
        if op.get('malformed'):
            await emit(None)
            return
    await emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': full,
        'usage': {'input_tokens': 11, 'output_tokens': 7, 'cache_read_input_tokens': 3},
        'total_cost_usd': 999, 'modelUsage': {model: {}}})


async def cli():
    argv = sys.argv[1:]
    config = json.loads(argv[argv.index('--mcp-config')+1])['mcpServers']['bob']
    request = json.loads(sys.stdin.readline())
    history = json.loads(request['message']['content'].split('\n', 1)[1])
    # Test fixtures supply the operations as the final user content.
    operations = json.loads(history[-1]['content'])
    process = await asyncio.create_subprocess_exec(config['command'], *config['args'],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    serial = 0
    async def rpc(method, params):
        nonlocal serial
        serial += 1
        process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': serial, 'method': method, 'params': params})+'\n').encode())
        await process.stdin.drain()
        return json.loads(await process.stdout.readline())
    async def emit(event):
        print(json.dumps(event) if event is not None else '{malformed', flush=True)
    try:
        await script(operations, emit, rpc)
    finally:
        process.stdin.close()
        await process.wait()


if __name__ == '__main__':
    asyncio.run(cli())
