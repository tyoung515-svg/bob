"""Installed MCP stdio controller, running only under a worker profile.

Each request owns a session and all its children. A bounded sequence supports
browser navigation and inspection in the same isolated browser session.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from contextlib import asynccontextmanager

import anyio
from anyio.streams.text import TextReceiveStream
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage


@asynccontextmanager
async def transport(args):
    """MCP stdio without the SDK's setsid/killpg policy.

    Keep the initial server in the job group. The daemon's subreaper also catches
    browser launchers that create their own groups; the SDK must not kill our
    controller's whole group during its independent shutdown procedure.
    """
    read, received = anyio.create_memory_object_stream(0)
    write, outgoing = anyio.create_memory_object_stream(0)
    process = await anyio.open_process(['/usr/bin/node', *args], env=dict(os.environ),
                                       stderr=sys.stderr, start_new_session=False)
    async def receive():
        buffer = ''
        async with read:
            async for chunk in TextReceiveStream(process.stdout):
                buffer += chunk
                if len(buffer.encode()) > 4 * 1024 * 1024:
                    raise ValueError('MCP message byte limit')
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    await read.send(SessionMessage(types.JSONRPCMessage.model_validate_json(line)))
    async def send():
        async with outgoing:
            async for item in outgoing:
                await process.stdin.send((item.message.model_dump_json(by_alias=True, exclude_none=True) + '\n').encode())
    async with anyio.create_task_group() as group, process, read, received, write, outgoing:
        group.start_soon(receive)
        group.start_soon(send)
        try:
            yield received, write
        finally:
            with anyio.CancelScope(shield=True):
                await process.stdin.aclose()
                with anyio.move_on_after(3):
                    await process.wait()
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            group.cancel_scope.cancel()


async def main():
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) - {'operation', 'server', 'name', 'arguments', 'calls'}:
        raise ValueError('invalid MCP request')
    if request['server'] == 'filesystem':
        args = ['/opt/bob-mcp/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js', str(Path.cwd())]
    elif request['server'] == 'browser':
        # This revision belongs to MCP's pinned Playwright, not a second CLI's
        # browser cache. Full Chromium needs a forbidden AF_UNIX singleton socket.
        binary = '/opt/bob-browsers/chromium_headless_shell-1191/chrome-linux/headless_shell'
        if not os.access(binary, os.X_OK):
            raise ValueError('installed browser headless shell unavailable')
        # --no-zygote: even with --no-sandbox the zygote needs a user namespace, which
        # the worker denies (RestrictNamespaces, seccomp unshare); every renderer/GPU
        # fork then aborts in sandbox/linux/services/credentials.cc. The job TMPDIR is
        # private to this job and owned by the worker.
        config = Path(os.environ.get('TMPDIR') or '/tmp') / 'bob-browser-config.json'
        config.write_text(json.dumps({'browser': {'launchOptions': {'args': ['--no-zygote']}}}))
        args = ['/opt/bob-mcp/node_modules/@playwright/mcp/cli.js', '--headless', '--isolated',
                '--no-sandbox', '--executable-path', binary, '--config', str(config)]
    else:
        raise ValueError('MCP server is not enrolled')
    async with transport(args) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if request['operation'] == 'list':
                result = [tool.model_dump(mode='json') for tool in (await session.list_tools()).tools]
            elif request['operation'] == 'call':
                result = (await session.call_tool(request['name'], request.get('arguments', {}))).model_dump(mode='json')
            elif request['operation'] == 'sequence':
                calls = request['calls']
                if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
                    raise ValueError('invalid MCP sequence')
                result = [(await session.call_tool(call['name'], call.get('arguments', {}))).model_dump(mode='json') for call in calls]
            else:
                raise ValueError('invalid MCP operation')
    print(json.dumps(result))


if __name__ == '__main__':
    asyncio.run(main())
