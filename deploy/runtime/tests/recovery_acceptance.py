"""S3g backend acceptance: two MARKED disposable installed lanes, no providers.

Native USB/print behavior is validated by the app half. This harness simulates
its verified-save acknowledgement. Fault boundaries run in test_recovery_journal
with real Linux storage; no production crash switch is installed.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

sys.path[:0]=['/opt/bob/bobclaw-core','/opt/bob/deploy/runtime/lib',str(Path(__file__).parent)]
from password_acceptance import command,http,pids,next_code,wait_gateway,broker,FAMILY


def secret():
    return base64.b32encode(secrets.token_bytes(32)).decode().rstrip('=')


def receipt(call,key):
    deadline=time.monotonic()+100
    while time.monotonic()<deadline:
        value=call('settings.status',{'request_key':key})
        if value.get('ok') and value['value']['phase'] in {'ready','failed'}:
            return value['value']
        time.sleep(.2)
    raise RuntimeError('receipt_timeout')


async def durable_probe(home,lane,epoch):
    """Own temporary Postgres schema; running executor cannot see these fake rows."""
    from common import read_env
    from urllib.parse import urlparse
    os.environ.update(read_env(home/'core.env'))
    os.environ.update(BOB_EXECUTION_BOUNDARY='worker',BOB_SETTINGS_ID=lane['lane_id'])
    import asyncpg
    from core.execution.api import lane_id
    from core.execution.store import Store
    from core.execution.models import Authority,LOOP_TOPOLOGY,canonical,ExecutionError
    dsn=os.environ['POSTGRES_URL']
    if urlparse(dsn).hostname not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('local_postgres_required')
    schema='s3g_'+uuid.uuid4().hex
    admin=await asyncpg.connect(dsn)
    pool=None
    try:
        await admin.execute('CREATE SCHEMA '+schema)
        pool=await asyncpg.create_pool(dsn,min_size=1,max_size=3,server_settings={'search_path':schema})
        await pool.execute('CREATE TABLE projects(id UUID PRIMARY KEY,user_id TEXT,is_archived BOOLEAN DEFAULT FALSE);'
                           'CREATE TABLE conversations(id UUID PRIMARY KEY,user_id TEXT,project_id UUID)')
        store=Store(pool,lane_id())
        await store.initialize()
        authority=Authority(lane_id(),lane['lane'],lane['lane'],{},'portal',credential_epoch=epoch)
        team=dict(schema_version=1,id='recovery-probe',topology=LOOP_TOPOLOGY,
                  roles={r:'fake' for r in ('manager','worker','critic')})
        await store.save_team(authority,team,None,'team',{'definition':team})
        team=await store.team(authority,team['id'])
        model=dict(backend='__never_dispatch__',model='fake',enabled=True,exact_calls=True)
        acceptance={'criteria':[{'id':'final','output_kind':'answer','required':True,
                    'checks':{'required_sections':['Conclusion'],'require_citations':True}}]}
        await store.start(authority,dict(team=team['id'],team_revision=1,task='S3G supplied evidence',evidence_ids=['S3G'],
            acceptance=acceptance,request_key='run',budget=dict(start_calls=10,wall_mode='none',wall_seconds=None)),
            dict(team=team,models={'fake':model},review_independence='same_model'))
        row=await store.claim('recovery-acceptance')
        for ordinal in (0,1):
            await pool.execute("INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,spec) VALUES ($1,$2,$3,$4,'worker',$5::jsonb)",
                row['lane'],row['owner'],row['id'],ordinal,canonical({'id':str(ordinal)}))
        attempt=await store.reserve_attempt(row,0,model,'S3G evidence')
    except BaseException:
        if pool:
            await pool.close()
        await admin.execute('DROP SCHEMA IF EXISTS '+schema+' CASCADE')
        await admin.close()
        raise
    closed=False
    async def close():
        nonlocal closed
        if not closed:
            closed=True
            await pool.close()
            await admin.execute('DROP SCHEMA IF EXISTS '+schema+' CASCADE')
            await admin.close()
    async def verify(record,prefix):
        try:
            record(prefix+'-durable-epoch',row['run_authority']['credential_epoch']==epoch)
            try:
                await store.charge_attempt(row,attempt)
            except ExecutionError as exc:
                record(prefix+'-old-run-charge-fenced',exc.code=='credential_epoch_changed')
            else:
                record(prefix+'-old-run-charge-fenced',False)
            try:
                await store.reserve_attempt(row,1,model,'S3G evidence')
            except ExecutionError as exc:
                record(prefix+'-old-run-reserve-fenced',exc.code=='credential_epoch_changed')
            else:
                record(prefix+'-old-run-reserve-fenced',False)
            record(prefix+'-old-run-no-charge',await pool.fetchval('SELECT consumed FROM agent_budget_accounts WHERE run_id=$1',row['id'])==0)
        finally:
            await close()
    return verify,close


async def run(names,record,cleanups):
    from common import read_env
    from settings import provision,STATE
    from worker import load_identity
    from lane import rotate_token
    from core.recovery_contract import digest
    # Validate BOTH markers before provisioning either lane.
    identities={}
    for name in names:
        if json.loads((FAMILY/name/'bob-memory/acceptance-disposable.json').read_text()).get('purpose')!='s2d-disposable':
            raise ValueError('disposable_marker_required')
        identities[name]=load_identity(name)
        if not all(k in identities[name] for k in ('gateway_uid','bridge_uid')):
            raise ValueError('settings_identity_required')
    for name in names:
        provision(identities[name],FAMILY/name/'bob-memory')
        command('systemctl','restart',f'bob-gateway@{name}.service')
        wait_gateway(int(read_env(FAMILY/name/'bob-memory/lane.env')['BOBCLAW_GATEWAY_PORT']))
        rotate_token(name)
    target=names[0]
    home=FAMILY/target/'bob-memory'
    lane=identities[target]
    state=STATE/lane['lane_id']
    port=int(read_env(home/'lane.env')['BOBCLAW_GATEWAY_PORT'])
    other_port=int(read_env(FAMILY/names[1]/'bob-memory/lane.env')['BOBCLAW_GATEWAY_PORT'])
    other_token=(Path('/var/lib/bob-portal')/(names[1]+'.token')).read_text().strip()
    old_stick=None
    canaries=[]
    # Bootstrap a recovery verifier on existing password/TOTP lanes, then exercise
    # all three pairs with copied previous stick rejection after every rotation.
    for index,pair in enumerate(['password_totp','password_stick','totp_stick','password_totp']):
        current=read_env(state/'credentials/current.env')
        password,seed=current['BOBCLAW_PASSWORD'],current['TOTP_SECRET']
        token=(Path('/var/lib/bob-portal')/(target+'.token')).read_text().strip()
        link=uuid.uuid4().hex
        portal={'Authorization':'Bearer '+token,'X-Bob-Broker-Link':link}
        # Never reuse a step an earlier mode (e.g. password acceptance) just spent.
        code,step=next_code(seed,int(time.time()/30))
        # Avoid reusing the replacement-seed proof spent by the previous commit.
        if index:
            code,step=next_code(seed,step)
        status,human=http(port,'/auth/login',{'password':password,'totp_code':code})
        record(pair+'-login',status==200)
        human_header={'Authorization':'Bearer '+human['access_token']}
        status,agent=http(port,'/auth/agent-token',{'scope':{},'faces':[]},human_header)
        record(pair+'-agent',status==200)
        status,challenge=http(port,'/auth/owner/begin',{'operation':'secrets.set','resource':{'provider':'deepseek'}},portal)
        record(pair+'-owner-begin',status==200)
        code,step=next_code(seed,step)
        status,owner=http(port,'/auth/owner/confirm',{'challenge_id':challenge['challenge_id'],'password':password,'totp':code},portal)
        record(pair+'-owner-confirm',status==200)
        import jwt
        owner_header={'Authorization':'Bearer '+owner['owner_bearer'],'X-Bob-Broker-Link':link,
            'X-Bob-Portal-Credential':jwt.decode(token,options={'verify_signature':False})['jti']}
        import aiohttp
        sockets=[]
        async with aiohttp.ClientSession() as session:
            for path,bearer in [('/ws/chat',human['access_token']),('/ws/chat',token),
                                ('/ws/chat',agent['access_token']),('/ws/approvals',human['access_token']),
                                ('/ws/monitor',human['access_token'])]:
                socket=await session.ws_connect(f'http://127.0.0.1:{port}'+path,headers={'Authorization':'Bearer '+bearer},autoping=False)
                await socket.ping(b's3g')
                message=await socket.receive(timeout=5)
                while message.type in {aiohttp.WSMsgType.TEXT,aiohttp.WSMsgType.BINARY}:
                    message=await socket.receive(timeout=5)
                record(pair+'-socket-live',message.type==aiohttp.WSMsgType.PONG)
                sockets.append(socket)
            verify_durable,cleanup=await durable_probe(home,lane,int(current['BOB_CREDENTIAL_EPOCH']))
            cleanups.append(cleanup)
            before=pids(names)
            with broker(target) as call:
                reply=call('auth.factors.binding',{'contract_version':2})
                record(pair+'-binding',reply.get('ok') is True)
                binding=reply['value']
                fresh=secret()
                replacement_id=uuid.uuid4().hex
                body=dict(contract_version=2,request_key=uuid.uuid4().hex,pair=pair,
                    replacement={'recovery_id':replacement_id,'verifier':digest(binding,replacement_id,fresh)})
                if pair in {'password_stick','password_totp'}:
                    body['password']=password
                if pair in {'password_totp','totp_stick'}:
                    code,step=next_code(seed,step)
                    body['totp']=code
                if pair in {'password_stick','totp_stick'}:
                    body['old_stick']=old_stick
                if pair=='password_stick':
                    body['new_seed']=base64.b32encode(secrets.token_bytes(20)).decode()
                if pair=='totp_stick':
                    body['new_password']='S3G-'+secrets.token_urlsafe(32)
                canaries.extend([fresh,body.get('new_seed',''),body.get('new_password','')])
                # Foreign lane must not receive usable proof/challenge.
                foreign={**body,'request_key':uuid.uuid4().hex}
                if old_stick:
                    denied=http(other_port,'/settings/factors/prepare',foreign,
                        {'Authorization':'Bearer '+other_token,'X-Bob-Broker-Link':'foreign-link'})
                    record(pair+'-mixed-lane-denied',denied[0] in {401,404,422,429})
                prepared=call('auth.factors.prepare',body)
                if not prepared.get('ok'):
                    safe=receipt(call,body['request_key'])
                    prepared={'ok':safe['phase']=='ready','value':safe.get('result')}
                record(pair+'-prepared',prepared.get('ok') is True)
                challenge=prepared['value']
                commit=dict(contract_version=2,request_key=uuid.uuid4().hex,challenge_id=challenge['challenge_id'],
                            material_saved=True,new_totp='')
                if 'new_seed' in body:
                    import pyotp
                    commit['new_totp']=pyotp.TOTP(body['new_seed']).now()
                result=call('auth.factors.commit',commit)
                safe=receipt(call,commit['request_key'])
                record(pair+'-committed',safe['phase']=='ready' and safe.get('result')==dict(changed=True,restarted=True))
                # Observe the helper's restart before the deliberate stop/start
                # below, which must not mask a missing automatic restart.
                after=pids(names)
                record(pair+'-only-target-gateway',before[(target,'gateway')]!=after[(target,'gateway')]
                    and all(before[k]==after[k] for k in before if k!=(target,'gateway')))
                epoch=int(read_env(state/'credentials/current.env')['BOB_CREDENTIAL_EPOCH'])
                code,generation=http(port,'/auth/generation')
                journal=json.loads((state/('op-'+safe['operation_id']+'.json')).read_text())
                record(pair+'-loaded-generation-ready',code==200
                    and generation==dict(ready=True,credential_epoch=epoch)
                    and journal.get('gateway_ready_epoch')==epoch and journal.get('portal_published') is True)
                # Safe receipts remain available from the surviving broker despite
                # revocation of the portal credential it loaded before reset.
                command('systemctl','stop',f'bob-gateway@{target}.service')
                try:
                    record(pair+'-receipt-gateway-down',receipt(call,commit['request_key'])==safe)
                finally:
                    command('systemctl','start',f'bob-gateway@{target}.service')
                    wait_gateway(port)
                record(pair+'-safe-replies',all(c not in json.dumps([prepared,result,safe]) for c in canaries if c))
            for socket in sockets:
                message=await socket.receive(timeout=5)
                while message.type in {aiohttp.WSMsgType.TEXT,aiohttp.WSMsgType.BINARY}:
                    message=await socket.receive(timeout=5)
                record(pair+'-socket-closed',message.type in {aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR})
                await socket.close()
        await verify_durable(record,pair)
        for label,bearer in [('human',human['access_token']),('portal',token),('agent',agent['access_token'])]:
            record(pair+'-'+label+'-revoked',http(port,'/auth/status',headers={'Authorization':'Bearer '+bearer})[0]==401)
        record(pair+'-refresh-revoked',http(port,'/auth/refresh',{'refresh_token':human['refresh_token']})[0]==401)
        record(pair+'-owner-revoked',http(port,'/auth/owner/status',headers=owner_header)[0]==403)
        record(pair+'-other-lane-live',http(other_port,'/auth/status',headers={'Authorization':'Bearer '+other_token})[0]==200)
        active=json.loads((state/'credentials/active.json').read_text())
        record(pair+'-audit',json.loads((state/('audit-'+active['operation_id']+'.json')).read_text())['factor_pair']==pair)
        # Use a fresh portal publisher result to demonstrate copied old-stick
        # rejection under the current epoch, without retrying the committed frame.
        if old_stick:
            current=read_env(state/'credentials/current.env')
            new_token=(Path('/var/lib/bob-portal')/(target+'.token')).read_text().strip()
            bad=dict(contract_version=2,request_key=uuid.uuid4().hex,pair='password_stick',password=current['BOBCLAW_PASSWORD'],
                old_stick=old_stick,new_seed=base64.b32encode(secrets.token_bytes(20)).decode(),
                replacement={'recovery_id':uuid.uuid4().hex,'verifier':'b'*64})
            status,reply=http(port,'/settings/factors/prepare',bad,{'Authorization':'Bearer '+new_token,'X-Bob-Broker-Link':'old-copy'})
            if status!=422:
                print(json.dumps({'probe':'s3g.'+pair+'-copied-old-stick-detail','status':status,
                                  'error':(reply or {}).get('error') if isinstance(reply,dict) else str(reply)[:200]}),flush=True)
            record(pair+'-copied-old-stick-denied',status==422)
            time.sleep(1.2)
        old_stick={**binding,'recovery_id':replacement_id,'secret':fresh}
    current=read_env(state/'credentials/current.env')
    code,step=next_code(current['TOTP_SECRET'])
    code,step=next_code(current['TOTP_SECRET'],step)
    status,login=http(port,'/auth/login',{'password':current['BOBCLAW_PASSWORD'],'totp_code':code})
    record('next-login-notice',status==200 and login.get('security_notice','').startswith('two-factor was reset on '))
    raw=''.join(p.read_text() for p in state.glob('*.json'))
    logs=command('journalctl','--no-pager','--since','-20min','-u',f'bob-gateway@{target}.service',
        '-u',f'bob-settings-control@{lane["lane_id"]}.service').stdout
    record('journals-and-logs-no-secret',all(c not in raw+logs for c in canaries if c))


def main():
    from lane import validate_name
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane-a',required=True,type=validate_name)
    parser.add_argument('--lane-b',required=True,type=validate_name)
    args=parser.parse_args()
    if os.geteuid()!=0 or args.lane_a==args.lane_b:
        parser.error('root and two distinct marked disposable lanes required')
    failures=[]
    def record(name,passed,**detail):
        print(json.dumps({'probe':'s3g.'+name,'passed':bool(passed),**detail}),flush=True)
        if not passed:
            failures.append(name)
    async def installed():
        cleanups=[]
        try:
            await run([args.lane_a,args.lane_b],record,cleanups)
        finally:
            for cleanup in reversed(cleanups):
                await cleanup()
    try:
        asyncio.run(installed())
    except Exception as exc:
        record('harness-'+type(exc).__name__,False,
               exception_type=type(exc).__name__,exception_message=str(exc))
    record('overall',not failures)
    return int(bool(failures))


if __name__=='__main__':
    raise SystemExit(main())
