#!/opt/bob/.venv/bin/python
"""Fresh disposable WSL acceptance. Genuine owner proof + worker; deterministic model.

Never run on a user lane. The host importer must mark this entire distro disposable.
Only an obviously inert provider key; no model calls. Auth stays inside the test guest.
"""
import argparse
import asyncio
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

sys.path.insert(0,'/opt/bob/bobclaw-core')
sys.path.insert(0,'/usr/local/lib/bob_runtime')
PYTHON='/opt/bob/.venv/bin/python'
LANE='dayone-a'
ROOT=Path('/var/lib/bob/family')/LANE/'bob-memory'
MODES=('builder','fixer','orchestrator','denial','stale','replay/early','unknown')
CALC='def add(a,b): return a+b\n'
TEST='import unittest\nfrom calc import add\nclass Cases(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n'


def skipped_unknown():
    return {'mode':'unknown','passed':None,'skipped':True,'evidence_ids':[],
        'reason':'No safe installed lost-reply injection: stopping the worker socket can strand a queued publication; '
                 'stopping only its service permits socket activation. No worker/transport or database state is forged.'}


def wait_core(env,timeout=120):
    # The gateway can listen before core has finished startup. This installed
    # liveness route performs no model discovery, owner mutation or provider call.
    deadline=time.monotonic()+timeout
    last='no response'
    while time.monotonic()<deadline:
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        connection=HTTPConnection('127.0.0.1',int(env['BOBCLAW_CORE_PORT']),timeout=min(2,remaining))
        try:
            connection.request('GET','/health')
            response=connection.getresponse()
            last='HTTP '+str(response.status)
            if response.status==200 and json.loads(response.read()).get('status')=='ok':return
        except (OSError,HTTPException,ValueError,AttributeError) as exc:
            last=type(exc).__name__
        finally:connection.close()
        time.sleep(min(.2,max(0,deadline-time.monotonic())))
    raise RuntimeError(f'Disposable core /health did not become ready within {timeout}s (last: {last})')


def configure_lane(http,owner_headers):
    from common import read_env
    from model import save_env
    from core.lane_config import read_lane
    # Pin loopback BEFORE secrets.set enrolls and restarts core. No real
    # provider may be hit, even while the disposable lane is coming up.
    values=read_env(ROOT/'lane.env')
    values['DEEPSEEK_BASE_URL']='http://127.0.0.1:9'
    save_env(ROOT/'lane.env',values)
    inert='bob-acceptance-inert-key-not-a-secret'
    receipt=http('/settings/secrets',{'contract_version':2,'provider':'deepseek',
        'key':inert,'request_key':'dayone-inert-'+uuid.uuid4().hex},owner_headers)
    assert receipt['provider']=='deepseek' and receipt['stored'] is True,'Inert key was not stored'
    # Explicit selection remains an operator action when reusing a test lane.
    completed=subprocess.run(['bob-model','set-default',LANE,'deepseek_v4_flash'],
        capture_output=True,text=True,timeout=120)
    if completed.returncode:raise RuntimeError('Disposable bob-model set-default failed; output withheld')
    values=read_env(ROOT/'lane.env')
    lane=read_lane(ROOT,values=values)
    assert lane.default_error() is None,lane.default_error()
    configured=lane.backends[lane.default_backend]
    assert lane.default_backend=='deepseek_v4_flash' and configured.model,'Inert backend enrollment failed'
    assert lane.values['DEEPSEEK_BASE_URL']=='http://127.0.0.1:9'
    return {'mechanism':'owner POST /settings/secrets enrolls; bob-model set-default selects; model.save_env pins loopback transport',
        'secret_operation_id':receipt['operation_id'],'default_backend':lane.default_backend,
        'model':configured.model,'base_url':lane.values['DEEPSEEK_BASE_URL']}


async def child(options):
    from common import read_env
    os.environ.clear()
    os.environ.update({'PATH':'/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                       'BOB_LANE_ROOT':str(ROOT),'BOB_SERVICE_ROLE':'core'})
    os.environ.update(read_env(ROOT/'core.env'))
    import asyncpg
    from core.config import config
    from core.execution.api import lane_id,policy_epoch
    from core.execution.admission import Admission
    from core.execution.models import Authority,ExecutionError,canonical,digest
    from core.execution.owner import current_epoch,require_owner
    from core.execution.store import Store
    from core.execution.executor import Executor
    from core.execution.worker_queue import WorkerQueue
    from core.execution.job_store import JobStore
    from core.execution.workbench import Workbench,configured_binding
    from core.execution.store import decoded
    from core.tool_jobs.service import Jobs
    from core.execution.workspaces import snapshot
    from core.worker.client import Transport,configuration,close_queues
    from core.lane_config import get_lane
    from core.faces.registry import get_default_registry
    cfg=configuration()
    pool=await asyncpg.create_pool(config.POSTGRES_URL,min_size=1,max_size=8)
    store=Store(pool,lane_id());await store.initialize()
    epoch=await current_epoch(lane_id(),LANE)
    normal=Authority(lane_id(),LANE,LANE,options['scope'],'portal',options['portal_id'],
                     policy_epoch(config.BOBCLAW_SECRET),epoch)
    owner=Authority(lane_id(),LANE,LANE,{},'human',options['owner_hash'],
        policy_epoch(config.BOBCLAW_SECRET),epoch,'owner_stepup','bob-owner-controls',
        tuple(options['owner_operations']),options['portal_id'],options['link'])
    # This is explicitly an injected model catalog/transport, not an enrolled provider.
    lane=get_lane()
    assert lane.default_error() is None,lane.default_error()
    configured=lane.backends.get(lane.default_backend)
    assert configured and configured.model,'Disposable lane default must have a configured model'
    setup=options['lane_setup']
    assert setup['default_backend']==lane.default_backend and setup['model']==configured.model,'Lane setup/binding mismatch'
    model={'fixture':{'label':'Installed acceptance fixture (injected text)','backend':lane.default_backend,
        'model':configured.model,
        'transport':'text','enabled':True,'exact_calls':True}}
    assert configured_binding(model,lane)=='fixture'
    async def catalog():return model
    admission=Admission(store,catalog);work=Workbench(admission)
    worker=JobStore(pool,lane_id());await worker.initialize()
    queue=WorkerQueue(worker,Transport(cfg),poll=.05);queue.start()
    calls=[];tasks=[];cancelling=False
    evidence={'mode':options['mode'],'project_id':options['project_id'],'passed':False,'evidence_ids':[options['project_id']],
        'model_transport':'deterministic fixture; no provider call','binding_id':'fixture',
        'configured_backend':lane.default_backend,'configured_model':model['fixture']['model'],'lane_setup':setup}
    async def dispatch(binding,prompt):
        assert binding['backend']==lane.default_backend and binding['model']==model['fixture']['model']
        calls.append(prompt)
        source=ROOT/'projects'/options['project_id']
        before={i['path']:i for i in snapshot(source)['manifest']}
        edits=[{'operation':'replace' if 'app/calc.py' in before else 'create','path':'app/calc.py',
                'pre_sha256':before.get('app/calc.py',{}).get('sha256'),'content':CALC}]
        if options['mode']!='fixer' and (options['mode']!='orchestrator' or cancelling):
            content=TEST
            if cancelling:
                # A real, bounded unittest keeps the installed worker in flight
                # long enough to observe and cancel it, without fake receipts.
                content='import time\n'+TEST.replace('self.assertEqual', 'time.sleep(30); self.assertEqual')
            edits.append({'operation':'create','path':'app/test_calc.py','pre_sha256':None,'content':content})
        return {'text':canonical({'edits':edits}),'usage':{}}

    async def pin(prepared,checks,revision):
        async with store.transaction() as c:
            raw=await c.fetchval('SELECT job_request FROM work_preparations WHERE lane=$1 AND owner=$2 AND id=$3',
                normal.lane,normal.owner,prepared['preparation_id'])
        request=decoded(raw);selected=request['work_recipe']
        assert prepared['recipe']==selected['id'] and prepared['recipe_revision']==revision
        assert selected['revision']==revision and selected['definition']['revision']==revision
        assert selected['definition']['checks']==checks and selected['digest']==digest(selected['definition'])
        assert request['acceptance']['criteria'][0]['checks']['required_check_ids']==checks
        if not cancelling:evidence['recipe_pin']=selected
        return selected

    async def prepare(suffix,recipe='python-basic',mode='builder',revision=1,checks=None):
        prepared=await work.command(normal,'work.prepare',{'project_id':options['project_id'],
            'task':'Correct addition: add(2,3)=5, with a unittest. '+suffix,
            'mode':mode,'recipe':recipe,'request_key':'prepare-'+options['mode']+'-'+suffix})
        evidence['evidence_ids'].append(prepared['preparation_id'])
        await pin(prepared,checks or ['python.compile','python.unittest'],revision)
        return prepared

    async def start(prepared,suffix):
        await require_owner(owner,'grants.issue')
        proposal=prepared['proposal']
        await store.issue_grant(owner,{'proposal_id':proposal['proposal_id'],'proposal_revision':proposal['revision'],
            'expected_revision':None,'request_key':'grant-'+options['mode']+'-'+suffix,'coverage_kind':'task',
            'allow_long_runs':True,'auto_escalate':False,'call_ceiling':10,'expires_at':None,
            'allow_extended_jobs':False,'allow_custom_job_limit':False,'job_seconds_ceiling':900,'job_auto_escalate':False})
        started=await work.start(normal,{'preparation_id':prepared['preparation_id'],
            'request_key':'start-'+options['mode']+'-'+suffix})
        evidence['evidence_ids'].extend([started['id'],started['run_id']])
        if not cancelling:evidence.update(job_id=started['id'],run_id=started['run_id'])
        return started

    async def execute(started):
        executor=Executor(store,admission,dispatch,poll=.05)
        row=await store.claim(executor.controller);assert row and row['id']==started['run_id']
        task=asyncio.create_task(executor.execute(row));tasks.append(task)
        return task

    async def status(started):
        return await work.command(normal,'work.status',{'job_id':started['id']})

    async def completed(started,task,checks):
        await asyncio.wait_for(asyncio.shield(task),120)
        state=await status(started)
        run=await store.get(normal,started['run_id'],0)
        assert state['status']=='completed' and run['run']['status']=='completed',state.get('error')
        checked=state['checked_proposal']
        assert checked['checks_passed'] and checked['required_checks']==checks
        assert len(checked['check_runs'])==len(checks)
        assert sorted(c['check_id'] for c in checked['check_runs'])==checks
        assert all(c['passed'] and c['cleanup']=='idle_proved' and c['source_unchanged'] for c in checked['check_runs'])
        assert checked['recipe']==evidence['recipe_pin']
        attempts=await pool.fetch("SELECT execution_id,(request->>'input')::jsonb->>'operation' AS operation,outcome,cleanup FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY execution_id",
            normal.lane,normal.owner,started['run_id'])
        executed=[dict(r) for r in attempts if r['operation'] in {'python.compile','python.unittest'}]
        assert sorted(r['operation'] for r in executed)==checks
        assert {r['execution_id'] for r in executed}=={c['execution_id'] for c in checked['check_runs']}
        assert all(r['outcome']=='succeeded' and r['cleanup']=='idle_proved' for r in executed)
        evidence['executed_checks']=executed
        evidence['checks']=[{k:c[k] for k in ('check_id','passed','cleanup','execution_id')} for c in checked['check_runs']]
        evidence['evidence_ids'].extend(c['execution_id'] for c in checked['check_runs'])
        return state,checked

    def publish_args(started,checked):
        return {'job_id':started['id'],'proposal_digest':checked['digest'],
            'expected_revision':checked['revision'],'request_key':'publish-'+options['mode']}

    async def publication_row(started):
        raw=await pool.fetchrow('SELECT state,receipt,operation_key FROM work_publications WHERE lane=$1 AND owner=$2 AND job_id=$3',
            normal.lane,normal.owner,started['id'])
        return {**dict(raw),'receipt':decoded(raw['receipt'])} if raw else None

    async def publish_attempts(started):
        rows=await pool.fetch('SELECT execution_id,outcome,cleanup FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND caller_group=$3 ORDER BY execution_id',
            normal.lane,normal.owner,'publish:'+started['id'])
        return [dict(r) for r in rows]

    async def expect_error(code,call):
        try:await call
        except ExecutionError as exc:
            assert (exc.code,exc.status)==(code,403 if code=='face_work_mode_denied' else 409),(exc.code,exc.status)
            return {'code':exc.code,'status':exc.status,'detail':exc.detail}
        raise AssertionError('Expected '+code)

    try:
        faces=get_default_registry()
        installed_faces=('builder','fixer','orchestrator')
        assert all(face in faces for face in installed_faces),'Installed work profiles are missing'
        assert all(faces.get_face(face).preferred_backend==lane.default_backend for face in installed_faces)
        evidence['installed_faces']=list(installed_faces)
        mode=options['mode']
        if mode=='denial':
            refusal=await expect_error('face_work_mode_denied',work.command(normal,'work.prepare',
                {'mode':'fixer','project_id':options['project_id'],'request_key':'denied'},face='builder'))
            evidence.update(passed=True,http=options['denials'],face_denial=refusal,model_calls=0)
            return evidence
        recipe='python-basic';revision=1;checks=['python.compile','python.unittest']
        if mode in {'builder','orchestrator'}:
            recipe='acceptance-python' if mode=='builder' else 'compile-only'
            if mode=='orchestrator':checks=['python.compile']
            saved=await work.command(normal,'work.recipe.save',{'name':recipe,'checks':checks,
                'expected_revision':None,'request_key':'recipe-'+mode+'-1'},face='orchestrator')
            assert saved['recipe']['revision']==1 and saved['started'] is False
            catalog=await work.command(normal,'work.catalog',{})
            assert next(r for r in catalog['recipes'] if r['id']==recipe)==saved['recipe']
            if mode=='orchestrator':
                saved=await work.command(normal,'work.recipe.save',{'name':recipe,'checks':checks,
                    'expected_revision':1,'request_key':'recipe-'+mode+'-2'},face='orchestrator')
                revision=2
                assert saved['recipe']['revision']==2
                catalog=await work.command(normal,'work.catalog',{})
                assert next(r for r in catalog['recipes'] if r['id']==recipe)==saved['recipe']
                evidence['catalog_revisions']=[1,2]
        prepared=await prepare('main',recipe,'fixer' if mode=='fixer' else 'builder',revision,checks)
        started=await start(prepared,'main')
        if mode=='replay/early':
            # No checked digest/revision exists before completion. Keep the SAME
            # request key, then fill those review fields from the final proposal.
            early={'job_id':started['id'],'proposal_digest':'0'*64,'expected_revision':1,
                   'request_key':'publish-'+mode}
            evidence['early']=await expect_error('work_not_completed',work.command(owner,'work.publish',early))
            assert await publication_row(started) is None
            assert await publish_attempts(started)==[]
            evidence['early'].update(publication_rows=0,worker_attempts=0)
        task=await execute(started)
        state,checked=await completed(started,task,checks)
        assert len(calls)==1
        if mode=='fixer':
            baseline=state['baseline']['baseline']
            assert baseline['passed'] is False and 'FAIL' in calls[0]
            assert baseline['failing_test_ids']==['test_add (test_calc.Cases.test_add)']
            final=next(c for c in checked['check_runs'] if c['check_id']=='python.unittest')
            assert final['baseline_comparison']['failure'] is None and final['baseline_already_passing'] is False
            assert checked['baseline_already_passing'] is False
            assert set(baseline['failing_test_ids'])<=set(final['baseline_comparison']['final']['passed_test_ids'])
            assert final['baseline_comparison']['final']['tests_run']>=baseline['tests_run']
            evidence.update(baseline=baseline,baseline_comparison=final['baseline_comparison'],baseline_already_passing=False)
            evidence['evidence_ids'].append(state['baseline']['execution_id'])
        if mode=='orchestrator':
            assert checked['required_checks']==['python.compile'] and len(checked['check_runs'])==1
            assert [f['path'] for f in checked['files']]==['app/calc.py']
            cancelling=True
            prepared=await prepare('cancel')
            cancelled=await start(prepared,'cancel')
            task=await execute(cancelled)
            async with asyncio.timeout(90):
                while True:
                    active=await pool.fetchval("SELECT execution_id FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome IN ('dispatch_intent','running') AND (request->>'input')::jsonb->>'operation'='python.unittest'",
                        normal.lane,normal.owner,cancelled['run_id'])
                    # The durable queue records dispatch_intent until terminal
                    # settlement; hello proves the daemon actually has this job.
                    if active:
                        observed=await asyncio.to_thread(queue.transport.hello)
                        if observed['active_execution']==active:break
                    assert not task.done(),'Cancel target finished before a running unittest was observed'
                    await asyncio.sleep(.05)
            initial=await Jobs(admission).command(normal,'jobs.cancel',{'job_id':cancelled['id'],'request_key':'cancel-orchestrator'})
            assert initial['status'] in {'cancelling','cancelled'},initial['status']
            await asyncio.wait_for(asyncio.shield(task),45)
            terminal=await Jobs(admission).command(normal,'jobs.cancel',{'job_id':cancelled['id'],'request_key':'cancel-orchestrator'})
            job=await status(cancelled);run=await store.get(normal,cancelled['run_id'],0)
            assert terminal['status']=='cancelled' and terminal['terminal'] is True
            assert job['status']=='cancelled' and run['run']['status']=='cancelled'
            rows=await pool.fetch('SELECT execution_id,outcome,cleanup FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3',
                normal.lane,normal.owner,cancelled['run_id'])
            assert rows and all(r['cleanup']=='idle_proved' and r['outcome'] in {'succeeded','cancelled'} for r in rows)
            assert any(r['execution_id']==active and r['outcome']=='cancelled' for r in rows)
            idle=await asyncio.to_thread(queue.transport.hello)
            assert idle['active_execution'] is None
            assert initial['cancellation_operation_id']==terminal['cancellation_operation_id']
            evidence.update(passed=True,model_calls=len(calls),cancel={'initial_status':initial['status'],
                'terminal_status':terminal['status'],'job_status':job['status'],'run_status':run['run']['status'],
                'job_id':cancelled['id'],'run_id':cancelled['run_id'],'active_observed':observed['active_execution'],
                'idle_observed':idle['active_execution'],
                'operation_id':terminal['cancellation_operation_id'],'active_execution_id':active,
                'attempts':[dict(r) for r in rows]})
            evidence['evidence_ids'].extend(r['execution_id'] for r in rows)
            return evidence
        args=publish_args(started,checked)
        if mode=='builder':assert {f['path'] for f in checked['files']}=={'app/calc.py','app/test_calc.py'}
        if mode=='fixer':assert [f['path'] for f in checked['files']]==['app/calc.py']
        if mode=='stale':
            source=ROOT/'projects'/options['project_id']
            (source/'unrelated.txt').write_text('Unrelated managed source change.\n')
            before=snapshot(source)['digest']
            attempts=await publish_attempts(started)
            first=await expect_error('proposal_stale',work.command(owner,'work.publish',args))
            row=await publication_row(started)
            assert row['state']=='conflict' and row['receipt']['code']=='proposal_stale'
            replay=await expect_error('proposal_stale',work.command(owner,'work.publish',args))
            assert await publication_row(started)==row
            after=await publish_attempts(started)
            assert attempts==after==[] and snapshot(source)['digest']==before
            readback=await status(started)
            assert readback['publication']=={'state':'conflict','receipt':row['receipt']}
            evidence.update(passed=True,model_calls=len(calls),publication=row,conflict=first,replay=replay,
                publication_worker_attempts_before=len(attempts),publication_worker_attempts_after=len(after))
            evidence['evidence_ids'].append(row['operation_key'])
            return evidence
        publication=await work.command(owner,'work.publish',args)
        actual=snapshot(ROOT/'projects'/options['project_id'])
        assert publication['state']=='published' and publication['output_digest']==actual['digest']==checked['output_digest']
        assert publication['tested_snapshot_digest']==actual['digest']
        row=await publication_row(started)
        assert row['state']=='completed' and row['receipt']==publication
        readback=await status(started)
        assert readback['publication']=={'state':'completed','receipt':publication}
        assert (ROOT/'projects'/options['project_id']/'app/calc.py').read_text()==CALC
        assert (ROOT/'projects'/options['project_id']/'app/test_calc.py').read_text()==TEST
        attempts=await publish_attempts(started)
        assert len(attempts)==1 and attempts[0]['outcome']=='succeeded' and attempts[0]['cleanup']=='idle_proved'
        if mode=='replay/early':
            assert args['request_key']==early['request_key']
            replay=await work.command(owner,'work.publish',args)
            assert replay=={**publication,'replayed':True}
            assert await publication_row(started)==row and await publish_attempts(started)==attempts
            evidence.update(replayed=True,early_same_key=True,
                early_review_fields_refreshed=['proposal_digest','expected_revision'])
        evidence.update(passed=True,model_calls=len(calls),publication={k:publication[k] for k in (
            'state','output_digest','tested_snapshot_digest','operation_key')},publication_state=row['state'],
            publication_worker_attempts=attempts,files=[i['path'] for i in actual['manifest'] if i['kind']=='file'])
        evidence['evidence_ids'].extend([publication['operation_key'],*(r['execution_id'] for r in attempts)])
        return evidence
    except Exception as exc:
        evidence.update(error=getattr(exc,'code',type(exc).__name__),detail=str(exc)[:2000],model_calls=len(calls))
        return evidence
    finally:
        try:
            # Also fence a start that committed but lost its reply to a timeout.
            # This project was created solely for this mode in the disposable lane.
            unfinished=await pool.fetch("SELECT id FROM agent_runs WHERE lane=$1 AND owner=$2 AND request->>'project_id'=$3 AND status NOT IN ('completed','cancelled','failed')",
                normal.lane,normal.owner,options['project_id'])
            for run in unfinished:
                await store.cancel(normal,run['id'],'cleanup-'+run['id'])
            for task in tasks:
                if not task.done():
                    try:await asyncio.wait_for(asyncio.shield(task),40)
                    except TimeoutError:
                        task.cancel();await asyncio.gather(task,return_exceptions=True)
        finally:
            try:await asyncio.to_thread(close_queues)
            finally:
                try:await queue.close()
                finally:await pool.close()


async def bounded_child(options):
    # Leave time for child()'s cancellation/queue finally blocks before runuser's
    # outer timeout. A stuck test must not leave an accepted real-model run.
    async with asyncio.timeout(180):
        return await child(options)


def root(modes):
    assert os.geteuid()==0
    assert json.loads(Path('/var/lib/bob/.dayone-disposable.json').read_text())['purpose']=='day-one-disposable'
    from common import read_env
    import jwt,pyotp
    os.environ['PATH']='/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    if not ROOT.exists():
        result=subprocess.run(['bob-lane','add',LANE,'--password-stdin'],input=secrets.token_urlsafe(32)+'\n',
            capture_output=True,text=True,timeout=180)
        assert result.returncode==0,'Disposable lane provisioning failed: '+result.stderr[-500:]
    env=read_env(ROOT/'lane.env')
    import socket
    def wait_gateway():
        end=time.monotonic()+60
        while True:
            try:
                with socket.create_connection(('127.0.0.1',int(env['BOBCLAW_GATEWAY_PORT'])),timeout=.5):return
            except OSError:
                if time.monotonic()>=end:raise RuntimeError('Disposable gateway did not become ready')
                time.sleep(.2)
    wait_gateway()
    cfg=json.loads((Path('/etc/bob/workers')/(env['BOB_WORKER_ID']+'.json')).read_text())
    portal=Path('/var/lib/bob-portal')/(LANE+'.token')
    token=portal.read_text().strip()
    claims=jwt.decode(token,options={'verify_signature':False}) # metadata only; HTTP verifies the actual bearer.
    link=uuid.uuid4().hex+uuid.uuid4().hex
    headers={'Authorization':'Bearer '+token,'X-Bob-Broker-Link':link,'Content-Type':'application/json'}
    def http(path,body,auth=headers,expected=(200,201,202)):
        connection=HTTPConnection('127.0.0.1',int(env['BOBCLAW_GATEWAY_PORT']),timeout=45)
        try:
            connection.request('POST',path,body=json.dumps(body),headers=auth)
            response=connection.getresponse();value=json.loads(response.read())
            assert response.status in expected,(path,response.status,value.get('error'))
            return value
        finally:connection.close()
    last_proof_step=-1
    def owner_proof(operation,resource):
        nonlocal last_proof_step
        from core.execution.owner import WORK_APPROVAL_OPERATIONS
        if operation in WORK_APPROVAL_OPERATIONS:
            challenge=http('/auth/owner/begin',{'contract_version':2,'operation':operation,'resource':resource})
            assert challenge.get('confirmation_mode')=='signed_in_user','Disposable work route did not offer signed-in confirmation'
            confirmed=http('/auth/owner/confirm',{'contract_version':2,'challenge_id':challenge['challenge_id'],'confirmation_mode':'signed_in_user'})
            bearer=confirmed['owner_bearer']
            return bearer,confirmed['owner_session'],{**headers,'Authorization':'Bearer '+bearer,'X-Bob-Portal-Credential':claims['jti']}
        # Owner confirmation shares login's TOTP high-water mark. Never submit
        # the same timestep again, even when modes finish in under 30 seconds.
        deadline=time.monotonic()+35
        while int(time.time()/30)<=last_proof_step:
            if time.monotonic()>deadline:raise RuntimeError('TOTP clock did not advance')
            time.sleep(.2)
        challenge=http('/auth/owner/begin',{'contract_version':2,'operation':operation,'resource':resource})
        proof_time=time.time()
        confirmed=http('/auth/owner/confirm',{'contract_version':2,'challenge_id':challenge['challenge_id'],
            'password':env['BOBCLAW_PASSWORD'],'totp':pyotp.TOTP(env['TOTP_SECRET']).at(proof_time)})
        last_proof_step=int(proof_time/30)
        bearer=confirmed['owner_bearer'];metadata=confirmed['owner_session']
        return bearer,metadata,{**headers,'Authorization':'Bearer '+bearer,'X-Bob-Portal-Credential':claims['jti']}

    # Stop the real model executor, as the stock checked-edits harness does. The
    # installed worker and actual queue/DB remain in use; only model text is injected.
    failures=[];lane_setup=None
    try:
        for mode in modes:
            if mode=='unknown':
                print(json.dumps(skipped_unknown()),flush=True);continue
            result={'mode':mode,'passed':False,'evidence_ids':[],'lane_setup':lane_setup}
            try:
                wait_core(env)
                if lane_setup is None:
                    _,_,setup_headers=owner_proof('secrets.set',{'provider':'deepseek'})
                    lane_setup=configure_lane(http,setup_headers)
                    wait_core(env);wait_gateway()
                result['lane_setup']=lane_setup
                pid=http('/projects',{'name':'Day-one '+mode})['id']
                result.update(project_id=pid,evidence_ids=[pid])
                # Owner sessions last five minutes; obtain fresh proof per mode.
                bearer,metadata,owner_headers=owner_proof('work.workspace.create',{'project_id':pid})
                created=http('/api/work/workspace.create',{'contract_version':2,'project_id':pid,'request_key':'workspace-'+mode},owner_headers)
                assert created['target']=='bob-lane-workspace'
                denials=[]
                if mode=='denial':
                    for operation,body in (
                        ('workspace.create',{'project_id':pid,'request_key':'denied-workspace'}),
                        ('publish',{'job_id':'0'*32,'proposal_digest':'0'*64,'expected_revision':1,'request_key':'denied-publish'})):
                        value=http('/api/work/'+operation,{'contract_version':2,**body},expected=(403,))
                        # A plain portal bearer on an exact owner route has no owner session.
                        assert value['error']['code']=='owner_session_invalid',value
                        denials.append({'route':'/api/work/'+operation,'status':403,'code':value['error']['code']})
                    value=http('/api/work/prepare',{'contract_version':2,'project_id':pid,'mode':'builder',
                        'recipe':'python-basic','task':'Denied prepare','request_key':'denied-prepare'},expected=(404,))
                    assert value['error']['code']=='unsupported_operation',value
                    denials.append({'route':'/api/work/prepare','status':404,'code':value['error']['code']})
                if mode=='fixer':
                    app=ROOT/'projects'/pid/'app';app.mkdir(mode=0o2770)
                    (app/'calc.py').write_text('def add(a,b): return a-b\n')
                    (app/'test_calc.py').write_text(TEST)
                    for path in (app,*app.iterdir()):
                        os.chown(path,cfg['service_uid'],cfg['worker_gid']);path.chmod(0o2770 if path.is_dir() else 0o660)
                options={'mode':mode,'project_id':pid,'scope':claims['scope'],'portal_id':claims['jti'],
                    'owner_hash':hashlib.sha256(bearer.encode()).hexdigest(),'owner_operations':metadata['allowed_operations'],
                    'link':link,'denials':denials,'lane_setup':lane_setup}
                try:
                    subprocess.run(['systemctl','stop','bob-core@'+LANE+'.service'],check=True)
                    completed=subprocess.run(['runuser','-u',cfg['service_user'],'--',PYTHON,'-I',str(Path(__file__).resolve()),'--child'],
                        input=json.dumps(options),capture_output=True,text=True,timeout=300,cwd='/')
                finally:
                    subprocess.run(['systemctl','start',*['bob-'+role+'@'+LANE+'.service' for role in ('core','gateway','bridge')]],check=True)
                if completed.returncode:raise RuntimeError('Installed '+mode+' child failed: '+completed.stderr[-2500:])
                result={**json.loads(completed.stdout),'lane_setup':lane_setup}
                assert result['mode']==mode and result['project_id']==pid
            except Exception as exc:
                result.update(passed=False,error=type(exc).__name__,detail=str(exc)[:2500])
            print(json.dumps(result),flush=True)
            if result['passed'] is not True:failures.append(mode)
    finally:
        subprocess.run(['systemctl','start',*['bob-'+role+'@'+LANE+'.service' for role in ('core','gateway','bridge')]],check=True)
    return 1 if failures else 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',default=','.join(MODES),help='Comma-separated modes: '+','.join(MODES))
    parser.add_argument('--child',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.child:
        options=json.load(sys.stdin)
        try:result=asyncio.run(bounded_child(options))
        except Exception as exc:
            result={'mode':options['mode'],'project_id':options['project_id'],'passed':False,'evidence_ids':[],
                    'error':getattr(exc,'code',type(exc).__name__),'detail':str(exc)[:2000]}
        print(json.dumps(result))
    else:
        modes=list(dict.fromkeys(mode.strip() for mode in args.mode.split(',')))
        if not modes or any(mode not in MODES for mode in modes):parser.error('Unknown mode; choose from '+','.join(MODES))
        if modes==['unknown']:
            print(json.dumps(skipped_unknown()))
        else:sys.exit(root(modes))
