"""Device login regression contracts. No live credentials or sign-in."""
from contextlib import contextmanager
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import struct

import pytest

from test_runtime_model_configured_worker import runtime, tmp_path, mock_redis, packaged_worker_mode


def test_worker_device_login_uses_leased_proxy_and_releases_on_failure(runtime,monkeypatch):
    model,_,root=runtime
    import worker
    from core.worker import contract, setup_network
    # Restore the production entry point hidden by the persistence-only fixture.
    import ast
    from pathlib import Path
    tree=ast.parse((Path(__file__).parents[2]/'deploy/runtime/lib/model.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='login_options')
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<login_options>','exec'),model.__dict__)
    config={'lane_id':'test','egress_port':21000}
    monkeypatch.setattr(contract,'root_json',lambda p:config)
    held=[]
    env={k:'http://bob:'+('a'*64)+'@127.0.0.1:21000' for k in
         ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')}
    env.update(NO_PROXY='',no_proxy='')
    @contextmanager
    def lease(cfg,deadline):
        assert cfg is config and 895 < deadline-time.monotonic() <= 900
        held.append(True)
        try: yield env
        finally: held.clear()
    monkeypatch.setattr(setup_network,'setup_codex_env',lease)
    monkeypatch.setattr(model,'prepare',lambda root:None)
    events=[]; monkeypatch.setattr(model,'emit',events.append)
    def popen(argv,**options):
        assert held and argv==[model.CODEX,'login','--device-auth']
        assert options['user']==options['group']==1
        assert options['extra_groups']==[]
        assert options['env']=={**model.codex_options(root)['env'],**env,
                                  'CODEX_HOME': options['env']['CODEX_HOME']}
        assert str(root) in options['env']['CODEX_HOME']
        assert options['env']['CODEX_HOME'] != str(root / 'codex-home')
        assert options['env']['NO_PROXY']==options['env']['no_proxy']==''
        raise OSError('sensitive process output must not be forwarded')
    monkeypatch.setattr(model.subprocess,'Popen',popen)
    assert model.openai_login('new-lane') is False
    assert not held
    assert events==[{'event':'error','code':'signin_unavailable','detail':model.LOGIN_ERRORS['signin_unavailable']}]


@pytest.mark.parametrize('status,output,code',[
    (1,'Error logging in: error sending request for url (https://auth.openai.com/api/accounts/deviceauth/usercode)','signin_unreachable'),
    (1,'HTTP 403 private-response','signin_refused'),
    (1,'device code expired private-response','signin_timeout'),
    (-15,'private-response','signin_cancelled'),
    (0,'private-response','signin_not_stored'),
    (1,'private-response','signin_failed'),
])
def test_login_reasons_never_forward_output(runtime,status,output,code,monkeypatch):
    model,_,_=runtime
    events=[]; monkeypatch.setattr(model,'emit',events.append)
    assert model.login_failure(status,output)==code
    model.login_error(code)
    assert 'private-response' not in json.dumps(events)
    assert set(events[0])=={'event','code','detail'}


async def test_setup_lease_enforces_fixed_policy_without_helper_callback():
    from core.builder.egress_proxy import Lease
    lease=Lease(None,None,time.time()+30,profile='codex',setup=True)
    for host in ('auth.openai.com','api.openai.com','chatgpt.com'):
        assert await lease.event({'type':'policy','domain':host,'method':'CONNECT'})=={'decision':'allow'}
    for host,method in [('example.com','CONNECT'),('sub.auth.openai.com','CONNECT'),('auth.openai.com','POST')]:
        assert await lease.event({'type':'policy','domain':host,'method':method})=={'decision':'deny'}
    lease.expiry=time.time()-1
    with pytest.raises(ValueError): await lease.event({'type':'policy','domain':'auth.openai.com','method':'CONNECT'})


async def test_setup_expiry_survives_backward_wall_clock(monkeypatch):
    from core.builder.egress_proxy import Lease
    lease=Lease(None,None,time.time()+30,profile='codex',setup=True)
    monkeypatch.setattr(time,'time',lambda:lease.expiry-1000)
    monkeypatch.setattr(time,'monotonic',lambda:lease.setup_deadline+1)
    with pytest.raises(ValueError): await lease.event({'type':'policy','domain':'auth.openai.com','method':'CONNECT'})


@pytest.mark.parametrize('uid,profile,seconds,extra,accepted',[
    (0,'codex',30,{},True), (42,'codex',30,{},False), (99,'codex',30,{},False),
    (0,'builder',30,{},False), (0,'mcp',30,{},False),
    (0,'codex',901,{},False), (0,'codex',-1,{},False),
    (0,'codex',30,{'hosts':['example.com']},False),
])
async def test_proxy_setup_admission_and_eof_revocation(monkeypatch,uid,profile,seconds,extra,accepted):
    from core.builder.egress_proxy import Proxy
    import socket
    monkeypatch.setattr(socket,'SO_PEERCRED',17,raising=False)
    reader=asyncio.StreamReader()
    class Writer:
        def __init__(self): self.data=b''; self.closed=False
        def get_extra_info(self,key):
            return SimpleNamespace(getsockopt=lambda *a:struct.pack('3i',123,uid,uid))
        def write(self,data): self.data+=data
        async def drain(self): pass
        def close(self): self.closed=True
        async def wait_closed(self): pass
    writer=Writer(); proxy=Proxy(42)
    reader.feed_data((json.dumps(dict(type='setup-codex',profile=profile,token='a'*64,
        expires_at=time.time()+seconds,**extra))+'\n').encode())
    task=asyncio.create_task(proxy.control(reader,writer))
    await asyncio.sleep(.01)
    assert bool(proxy.leases)==accepted
    if accepted:
        lease=next(iter(proxy.leases.values()))
        assert lease.setup and lease.profile=='codex'
        assert json.loads(writer.data)=={'ready':True}
        tunnel=asyncio.create_task(asyncio.Event().wait())
        lease.tasks.add(tunnel)
    reader.feed_eof()
    await asyncio.wait_for(task,1)
    assert not proxy.leases and writer.closed
    if accepted:
        await asyncio.sleep(0)
        assert lease.closed and tunnel.cancelled()


@pytest.mark.parametrize('failure,code',[
    ('timeout','signin_timeout'), ('cancel','signin_cancelled'),
    ('missing','signin_not_stored'), ('failure','signin_failed'),
])
def test_device_login_cleanup_and_fixed_events(runtime,monkeypatch,failure,code):
    model,_,root=runtime
    monkeypatch.setattr(model,'prepare',lambda root:None)
    held=[]
    @contextmanager
    def options(root,deadline):
        held.append(True)
        try: yield {'env':{}}
        finally: held.clear()
    monkeypatch.setattr(model,'login_options',options)
    class Process:
        stdout=SimpleNamespace()
        killed=False
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def poll(self): return None if failure in {'timeout','cancel'} and not self.killed else 0
        def kill(self): self.killed=True
        def wait(self,**kw):
            if 'timeout' in kw:
                if failure=='timeout': raise model.subprocess.TimeoutExpired('codex login',900)
                if failure=='cancel': raise KeyboardInterrupt
            return 1 if failure=='failure' else 0
    class Selector:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def register(self,*args): pass
        def get_map(self): return {}
    process=Process()
    monkeypatch.setattr(model.subprocess,'Popen',lambda *a,**kw:process)
    monkeypatch.setattr(model.selectors,'DefaultSelector',Selector)
    monkeypatch.setattr(model,'openai_credentials',lambda root:(False,False))
    share=Mock(); monkeypatch.setattr(model,'share_auth',share)
    events=[]; monkeypatch.setattr(model,'emit',events.append)
    assert not model.openai_login('new-lane')
    assert not held and events==[{'event':'error','code':code,'detail':model.LOGIN_ERRORS[code]}]
    assert process.killed==(failure in {'timeout','cancel'})
    share.assert_not_called()


@pytest.mark.skipif(sys.platform!='linux',reason='requires Linux root and installed live lane; opt in with BOB_BU9_LIVE_LANE')
def test_installed_worker_boundary_no_signin():
    import os
    import subprocess
    from pathlib import Path
    lane=os.environ.get('BOB_BU9_LIVE_LANE')
    if os.geteuid()!=0 or not lane:
        pytest.skip('set BOB_BU9_LIVE_LANE as Linux root to inspect the installed lane')
    script=Path(__file__).parents[2]/'deploy/runtime/tests/bu9_network_acceptance.py'
    result=subprocess.run([sys.executable,str(script),'--lane',lane],capture_output=True,text=True,timeout=60)
    assert result.returncode==0, 'Installed boundary check failed; captured output is withheld.'
    assert 'PASS:' in result.stdout
