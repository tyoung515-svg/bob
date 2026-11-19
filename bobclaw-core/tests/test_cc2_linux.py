"""Linux subprocess, real shim and Landlock negative-channel checks; no account."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from core.backends.claude_turn import ClaudeTurn
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux profile/channel checks')
CORE = Path(__file__).resolve().parents[1]

HARNESS = r'''
import json, socket, sys
from pathlib import Path
from core.worker import claude_turn as worker
from core.worker import landlock
original = worker.invocation
real_confine = landlock.install
def confine(*args, **kwargs):
    kwargs['readonly'] = (str(Path(sys.argv[3]).parents[1]),)
    real_confine(*args, **kwargs)
landlock.install = confine
def invocation(value, requests, responses):
    argv = original(value, requests, responses)
    at = argv.index('--mcp-config')+1
    mcp = json.loads(argv[at])
    mcp['mcpServers']['bob']['command'] = sys.executable
    mcp['mcpServers']['bob']['args'][1] = str(Path(sys.argv[3]).parents[1]/'core/worker/claude_mcp.py')
    argv[at] = json.dumps(mcp)
    return [sys.executable, sys.argv[3], *argv[1:]]
worker.invocation = invocation
worker.policy_ready = lambda config: None  # No installed guardian in unit fixtures.
conn = socket.socket(fileno=int(sys.argv[1]))
config = json.loads(sys.argv[2])
worker.supervise(conn, worker.frame(conn), config)
'''


@pytest.mark.asyncio
async def test_fake_process_cli_real_shim_adapter_and_environment(tmp_path, monkeypatch):
    monkeypatch.setattr('core.lane_config.require_backend', lambda *a: None)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-inherit')
    home = tmp_path/'claude'; home.mkdir(mode=0o700)
    config = {'claude_home': str(home), 'egress_port': 18081,
              'profiles': {'claude': {'argv': ['/usr/local/bin/claude'], 'network': True, 'egress': 'lane-proxy/1'}}}
    parent, child = socket.socketpair()
    env = {**os.environ, 'PYTHONPATH': str(CORE)}
    process = subprocess.Popen([sys.executable, '-B', '-c', HARNESS, str(child.fileno()), json.dumps(config),
        str(CORE/'tests/cc2_fake_cli.py')], pass_fds=(child.fileno(),), env=env)
    child.close()
    async def connector():
        return await asyncio.open_connection(sock=parent, limit=4*1024*1024+1)
    @tool
    async def get_server_time() -> str:
        """Read test time."""
        return 'time'
    records = []
    turn = ClaudeTurn(model='sonnet', tools=[get_server_time], connector=connector, recorder=lambda *r: records.append(r))
    # The fixture uses the normal fixed profile proxy validation, without network.
    original_send = turn.send
    async def send(value):
        if 'version' in value: value['network_proxy'] = 'http://bob:'+'a'*64+'@127.0.0.1:18081'
        await original_send(value)
    turn.send = send
    ops = [{'tools': [{'name': 'get_server_time'}]}, {'text': 'Done'}]
    messages = [HumanMessage(content=json.dumps(ops))]
    try:
        response = await asyncio.wait_for(turn.ainvoke(messages), 5)
        assert response.tool_calls[0]['name'] == 'get_server_time'
        request = response.tool_calls[0]
        turn.tool_started(request['name'], request['args'], request['id'])
        result = await get_server_time.ainvoke(request['args'])
        turn.accept_result(request['id'], result)
        messages.extend([response, ToolMessage(content=result, tool_call_id=request['id'])])
        final = await asyncio.wait_for(turn.ainvoke(messages), 5)
        assert final.content == 'Done' and final.usage_metadata['input_tokens'] == 14
    finally:
        await turn.close()
        if process.poll() is None:
            await asyncio.to_thread(process.wait, 7)
        parent.close()
    assert process.returncode == 0 and turn.cleanup == 'idle_proved'
    assert any(kind == 'tool_result' for _, kind, _ in records)


def test_other_process_and_proc_fd_cannot_open_turn_pipe(tmp_path):
    from core.worker.landlock import abi
    try: abi()
    except RuntimeError: pytest.skip('Landlock ABI3 unavailable; channel gate not established')
    private = tmp_path/'other-turn'; private.mkdir(mode=0o700)
    own = tmp_path/'own'; own.mkdir()
    pipe = private/'requests'; os.mkfifo(pipe, 0o600)
    fd = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
    code = '''
import os,sys
from core.worker.landlock import install
from core.worker.restrict import install as restrict
install(sys.argv[1],sys.argv[1])
restrict(network=False)
for target in sys.argv[2:]:
    try:
        fd=os.open(target,os.O_RDONLY|os.O_NONBLOCK)
    except PermissionError:
        continue
    else:
        os.close(fd)
        raise SystemExit('another turn pipe was accessible')
'''
    try:
        result = subprocess.run([sys.executable, '-B', '-c', code, str(own), str(pipe),
            '/proc/'+str(os.getpid())+'/fd/'+str(fd)], env={**os.environ, 'PYTHONPATH': str(CORE)}, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr + result.stdout
    finally:
        os.close(fd)
