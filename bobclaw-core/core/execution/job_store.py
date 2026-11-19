"""Durable queue mutations serialized by a per-lane transaction lock.

The physical controller additionally holds a session advisory lock through
recovery, execution and cleanup. Losing that DB connection cannot free a child:
the next controller fences the daemon and obtains a new idle barrier first.

Lock order: the controller's session lock (controller only), then the lane's
shared grant/policy transaction fence, then the worker-queue advisory lock,
then row locks. Runtime attempt/run mutations here share the queue lock, so differing accesses
to slot/attempt/run/account rows cannot invert across those mutations.
Policy edits and grant mutations take the
SAME fence exclusively before any rows; never acquire the fence after queue or
row locks. The policy fence is an alias of the grant fence, not a second key.
Continuation keeps this order in its outer transaction before nested admission.
"""
from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
import time

from .models import canonical, digest, ExecutionError
from .job_policy import (Reconciliation, authorize, effective_limit, next_tier, permits_job)
from core.worker.contract import MAX_TIMEOUT
from core.worker.contract import WorkerUnavailable, request_digest, validate, validate_v2

MIGRATION = Path(__file__).resolve().parents[3] / 'migrations/2026-11-03_worker_queue.sql'
TERMINAL = {'succeeded', 'failed', 'timed_out', 'cancelled', 'unknown'}


def decoded(row):
    if row is None:
        return None
    row = dict(row)
    for key in ('request', 'receipt', 'reconciliation_evidence'):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


class JobStore:
    def __init__(self, pool, lane, *, principal_limit=32, lane_limit=256, clock=time.time, workspace_verifier=None):
        self.pool, self.lane, self.clock = pool, lane, clock
        self.workspace_verifier = workspace_verifier
        self.principal_limit, self.lane_limit = principal_limit, lane_limit
        self.lock = int(digest([lane, 'worker-queue'])[:15], 16)
        self.controller_lock = int(digest([lane, 'physical-worker'])[:15], 16)

    async def initialize(self):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([self.lane,'grant-fence'])[:15],16))
            await c.execute('SELECT pg_advisory_xact_lock($1)', 0x424F42534348454D)
            await c.execute(MIGRATION.with_name('2026-11-03_settings_policy.sql').read_text(encoding='utf-8'))
            await c.execute(MIGRATION.read_text(encoding='utf-8'))
            await c.execute(MIGRATION.with_name('2026-11-03_worker_time_ladder.sql').read_text(encoding='utf-8'))
            await c.execute('INSERT INTO worker_lane_slots(lane) VALUES($1) ON CONFLICT DO NOTHING', self.lane)

    @asynccontextmanager
    async def connection(self, existing=None):
        if existing is not None:
            yield existing
        else:
            async with self.pool.acquire() as c:
                yield c

    async def grant_fence(self, c):
        await c.execute('SELECT pg_advisory_xact_lock_shared($1::bigint)', int(digest([self.lane, 'grant-fence'])[:15],16))

    async def authority(self, c, *, context=None, row=None):
        run_id = context.run_id if context else row['run_id']
        if not run_id:
            group=context.group if context else row['caller_group']
            if group.startswith('publish:'):
                from .publication import authorize_worker
                await authorize_worker(c,self.lane,context=context,row=row)
            return None, None
        owner = context.owner if context else row['owner']
        generation = context.run_generation if context else row['run_generation']
        boundary = context.boundary if context else row['boundary']
        raw = await c.fetchrow('SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE', self.lane, owner, run_id)
        from .store import Store, record
        run = record(raw) if raw else None
        statuses={'running','pausing'} if run and run.get('topology')=='workflow/v1' else {'running'}
        if not run or run['generation'] != generation or run['status'] not in statuses or run['authorization_revoked'] or run['boundary'] != boundary:
            raise WorkerUnavailable('stale owning run')
        grant = None
        if run.get('selected_grant_id') or run['request'].get('job_coverage'):
            try:
                grant = await Store(self.pool, self.lane)._grant(c, run)
            except ExecutionError as exc:
                raise WorkerUnavailable(exc.code) from exc
        if run['run_authority'].get('credential_epoch') is not None:
            from .models import Authority
            from .owner import check_epoch
            try:
                await check_epoch(Authority(**run['run_authority']))
            except ExecutionError as exc:
                raise WorkerUnavailable(exc.code) from exc
        return run, dict(grant) if grant else None

    async def coverage_request(self, c, run, request):
        if run and run.get('topology')=='workflow/v1':
            state=run.get('workflow_state')
            state=json.loads(state) if isinstance(state,str) else state
            project=request['cwd'].split('/')[0]
            scratch=state.get('check_workspaces',{}).get(request['workspace'])
            live=request['workspace']=='projects' and project==run['request']['project_id']
            if request['profile']!='builder' or request['args'] or not (live or scratch and request['cwd']==''):
                raise WorkerUnavailable('job outside the workflow workspace')
            if scratch:
                value=json.loads(request['input'])
                args=value.get('args',{})
                if value.get('operation')!='shell_run' or not any(
                    check['command']==args.get('command') and check['cwd']==args.get('cwd','') and
                    check['timeout_seconds']==args.get('timeout_s') and args.get('network') is False
                    for check in state['coverage']['checks']):
                    raise WorkerUnavailable('check outside the workflow grant')
            return request
        if not request['workspace'].startswith('run_'):
            return request
        if not run:
            raise WorkerUnavailable('scoped job owning run required')
        job = await c.fetchrow('SELECT * FROM tool_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3',
                               self.lane, run['owner'], run['id'])
        checked = bool(run['request'].get('worker_contract'))
        if checked:
            link = await c.fetchrow('SELECT * FROM checked_step_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3 AND workspace_id=$4 AND adapter_input_digest=$5',
                                    self.lane,run['owner'],run['id'],request['workspace'],digest(request['input']))
            if not link or request['profile'] != 'checked' or request['args']:
                raise WorkerUnavailable('checked step intent required')
        if not job or (not checked and job['workspace_id'] != request['workspace']) or request['profile'] != ('checked' if checked else 'proposal') or request['cwd']:
            raise WorkerUnavailable('scoped job workspace mismatch')
        from core.tool_jobs.service import Jobs
        from .models import Authority
        from .store import Store
        jobs = Jobs(type('AdmissionView', (), {'store': Store(self.pool,self.lane)})())
        folder = await jobs.attachment(Authority(**run['run_authority']), job['attachment_id'])
        registration = json.loads(job['registration']) if isinstance(job['registration'],str) else job['registration']
        if registration != folder or folder['attachment_id'] not in run['request']['job_coverage']['attachments']:
            raise WorkerUnavailable('scoped job registration changed')
        return {**request, 'workspace':'projects', 'cwd':folder['path'][len('projects/'):]}

    async def account(self, c, run):
        await c.execute('INSERT INTO run_job_accounts(lane,owner,run_id,max_job_attempts,job_seconds_budget) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',
                        self.lane, run['owner'], run['id'], run['request'].get('budget', {}).get('max_job_attempts',32), run['request'].get('budget', {}).get('job_seconds_budget'))
        return dict(await c.fetchrow('SELECT * FROM run_job_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3 FOR UPDATE', self.lane, run['owner'], run['id']))

    async def pause(self, c, row, reason):
        if row['run_id']:
            raw=await c.fetchrow('SELECT topology,workflow_state FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3',
                                self.lane,row['owner'],row['run_id'])
            if raw and raw['topology']=='workflow/v1':
                from .store import Store
                from .workflows import WorkflowService
                from .models import Authority
                from types import SimpleNamespace
                record=decoded(raw['workflow_state'])
                record['execution_error']=reason
                service=WorkflowService(SimpleNamespace(store=Store(self.pool,self.lane)))
                record['run']['reason']=reason
                await service.event(c,Authority(**record['initiator']),record,'run_state',{'status':record['run']['status'],'reason':reason})
                await self.event(c,row,'job_paused',{'reason':reason,'owner_flag':True})
                return
            await c.execute("UPDATE agent_runs SET status='paused',reason=$4,owner_flag=TRUE,revision=revision+1 WHERE lane=$1 AND owner=$2 AND id=$3 AND status NOT IN ('completed','cancelled','failed','paused')",
                            self.lane, row['owner'], row['run_id'], reason)
        await self.event(c, row, 'job_paused', {'reason':reason, 'owner_flag':True})

    async def enforce_active(self, c, row):
        async with c.transaction():
            await self.grant_fence(c)
            await self.policy_fence(c)
            await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
            try:
                if not await self.policy_allows(c, row['owner'], row['request']):
                    raise WorkerUnavailable('current owner policy requires confirmation')
                run, grant = await self.authority(c, row=row)
                await self.coverage_request(c, run, row['request'])
                authorize(row['selected_seconds'], grant, explicit=row['explicit_policy'], continuation=bool(row['parent_execution_id']), force_custom=row['custom_limit'])
                if (row['grant_id'],row['grant_revision']) != ((grant['id'],grant['revision']) if grant else (None,None)):
                    raise WorkerUnavailable('pinned_job_grant_changed')
                if run and run['deadline'] and run['deadline'].timestamp() <= self.clock():
                    raise WorkerUnavailable('run_wall_exhausted')
                return True
            except (WorkerUnavailable, ExecutionError) as exc:
                status = await c.fetchval('SELECT status FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3',self.lane,row['owner'],row['run_id']) if row['run_id'] else None
                if status != 'cancelling':
                    await self.pause(c, row, str(exc))
                await c.execute('UPDATE worker_job_attempts SET cancel_requested=TRUE WHERE lane=$1 AND execution_id=$2', self.lane,row['execution_id'])
                if row['run_id']:
                    await c.execute('UPDATE agent_runs SET cleanup_pending=TRUE WHERE lane=$1 AND owner=$2 AND id=$3',self.lane,row['owner'],row['run_id'])
                return False

    async def block_timeout(self, context, execution_id, reason):
        row = await self.get(execution_id)
        if row and (row['owner'],row['principal'],row['caller_group'],row['boundary'],row['run_id'],row['run_generation']) == (context.owner,context.principal,context.group,context.boundary,context.run_id,context.run_generation):
            async with self.pool.acquire() as c, c.transaction():
                await self.grant_fence(c)
                await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
                await self.pause(c,row,reason)

    def verified_workspace(self, row, expected):
        try:
            return self.workspace_verifier is not None and self.workspace_verifier(row) == expected
        except (OSError,ValueError,KeyError) as exc:
            raise WorkerUnavailable('workspace verification unavailable') from exc

    async def continue_timeout(self, context, execution_id, proof, *, current_workspace_digest):
        try:
            return await self._continue_timeout(context, execution_id, proof, current_workspace_digest=current_workspace_digest)
        except (WorkerUnavailable, ExecutionError) as exc:
            await self.block_timeout(context,execution_id,str(exc))
            raise

    async def _continue_timeout(self, context, execution_id, proof, *, current_workspace_digest):
        """Internal trusted-adapter entry point; no model/public JSON proof route.

        The adapter freezes/hashes the current workspace and establishes effects
        before calling, and supplies the still-current hash under its workspace
        lock. It must keep that lock until admission. Unknown effects cannot pass.
        """
        if not isinstance(proof, Reconciliation) or not proof.established(current_workspace_digest):
            raise WorkerUnavailable('workspace/effects reconciliation required')
        from .jobs import new_execution_id
        async with self.pool.acquire() as c, c.transaction():
            await self.grant_fence(c)
            await self.policy_fence(c)
            await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
            parent = decoded(await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2 FOR UPDATE',self.lane,execution_id))
            if not parent or (parent['owner'],parent['principal'],parent['caller_group'],parent['boundary'],parent['run_id'],parent['run_generation']) != (context.owner,context.principal,context.group,context.boundary,context.run_id,context.run_generation):
                raise WorkerUnavailable('stale continuation owner')
            child = decoded(await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND parent_execution_id=$2',self.lane,execution_id))
            if child:
                await self.authority(c, context=context)
                if child['reconciliation_evidence'] != asdict(proof):
                    raise WorkerUnavailable('continuation reconciliation conflict')
                return child
            if parent['outcome'] != 'timed_out' or parent['cleanup'] != 'idle_proved' or parent['cancel_requested']:
                raise WorkerUnavailable('known timeout and cleanup required')
            if parent['limiting_reason'] != 'selected_limit':
                raise WorkerUnavailable('timeout exhausted ' + str(parent['limiting_reason']))
            if not parent['auto_escalate']:
                raise WorkerUnavailable('job_escalation_disabled')
            if not self.verified_workspace(parent,proof.workspace_digest):
                raise WorkerUnavailable('current workspace reconciliation not established')
            request = {k:v for k,v in parent['request'].items() if k in {'profile','workspace','cwd','args','input','output_limit'}}
            request.update(version=1,job_id=new_execution_id(),timeout=next_tier(parent['selected_seconds']))
            # Validation of adapter-shaped requests uses real wall time for expiry.
            request['expires'] = time.time() + request['timeout'] + 20
            child = await self.admit(context,new_execution_id(),request,_connection=c,_parent=parent,_proof=asdict(proof))
            await c.execute("UPDATE worker_job_attempts SET reconciliation='established',reconciliation_evidence=$3::jsonb WHERE lane=$1 AND execution_id=$2",self.lane,execution_id,canonical(asdict(proof)))
            await self.event(c,parent,'timeout_continued',{'execution_id':child['execution_id'],'selected_seconds':child['selected_seconds'],'workspace_digest':proof.workspace_digest})
            return child

    async def event(self, c, row, kind, detail=None):
        await c.execute('INSERT INTO worker_job_events(lane,owner,execution_id,kind,detail) VALUES($1,$2,$3,$4,$5::jsonb)',
                        self.lane, row['owner'], row['execution_id'], kind, canonical(detail or {}))

    async def admit(self, context, execution_id, request, *, _connection=None, _parent=None, _proof=None):
        admission_digest = digest({'request': {k: v for k, v in request.items() if k not in {'job_id', 'expires'}},
                                   'policy': asdict(context.policy) if context.policy else None,
                                   'parent': _parent['execution_id'] if _parent else None})
        async with self.connection(_connection) as c, c.transaction():
            await self.grant_fence(c)
            await self.policy_fence(c)
            await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
            old = await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2', self.lane, execution_id)
            if old:
                # Pre-ladder rows hashed only the request. Accept that receipt
                # format solely for an unchanged legacy admission; it cannot
                # authorize a new policy or continuation under the same ID.
                legacy_digest = digest({k: v for k, v in request.items() if k not in {'job_id', 'expires'}})
                same_digest = old['admission_digest'] == admission_digest or (
                    context.policy is None and _parent is None and not old['explicit_policy']
                    and old['parent_execution_id'] is None and old['admission_digest'] == legacy_digest)
                if (old['owner'], old['principal'], old['caller_group'], old['boundary'], old['run_id'], old['run_generation']) != (context.owner, context.principal, context.group, context.boundary, context.run_id, context.run_generation) or not same_digest:
                    raise WorkerUnavailable('worker admission conflict')
                return decoded(old)
            validate(request, max_timeout=MAX_TIMEOUT)
            if not await self.policy_allows(c, context.owner, request):
                raise WorkerUnavailable('current owner policy requires confirmation')
            if request['profile'] == 'auth-replace':
                raise WorkerUnavailable('auth publication uses separate socket')
            paused = await c.fetchval('SELECT paused_unknown FROM worker_callers WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4',
                                     self.lane, context.owner, context.principal, context.group)
            if paused and not context.run_id and context.group.startswith('publish:'):
                # Only this installed deterministic adapter can reconcile an
                # unknown publication. Never unpause generic worker callers.
                await self.authority(c,context=context)
                publication = await c.fetchrow('SELECT state,operation_key FROM work_publications WHERE lane=$1 AND owner=$2 AND job_id=$3',
                                              self.lane,context.owner,context.group.removeprefix('publish:'))
                prior = await c.fetch('SELECT * FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4',
                                      self.lane,context.owner,context.principal,context.group)
                value = json.loads(request['input']) if isinstance(request['input'],str) else request['input']
                if (publication and publication['state']=='unknown' and request['profile']=='checked'
                        and not request['args'] and value.get('operation')=='workspace.edit'
                        and value.get('operation_key')==publication['operation_key'] and prior
                        and all(p['outcome'] in TERMINAL and p['cleanup']=='idle_proved'
                                and p['boundary']==context.boundary and p['run_id'] is None
                                and all(decoded(p)['request'][k]==request[k] for k in ('profile','workspace','cwd','args','input'))
                                for p in prior)):
                    # The directory flock + journal, not this queue reset,
                    # establish whether the edit can resume without conflicts.
                    await c.execute('UPDATE worker_callers SET paused_unknown=FALSE WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4',
                                    self.lane,context.owner,context.principal,context.group)
                    paused=False
            if paused:
                raise WorkerUnavailable('worker caller group paused: unknown outcome requires reconciliation')
            blocked = await c.fetchval('SELECT paused_timeout FROM worker_callers WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4',
                                      self.lane, context.owner, context.principal, context.group)
            if blocked and (not _parent or blocked != _parent['execution_id']):
                raise WorkerUnavailable('worker caller group paused: timeout requires reconciliation')
            count = await c.fetchval("SELECT count(*) FROM worker_job_attempts WHERE lane=$1 AND principal=$2 AND outcome IN ('queued','dispatch_intent','running')", self.lane, context.principal)
            total = await c.fetchval('SELECT count(*) FROM worker_queue WHERE lane=$1', self.lane)
            if count >= self.principal_limit or total >= self.lane_limit:
                raise WorkerUnavailable('worker admission bound exceeded')
            policy = context.policy
            selected = policy.selected_seconds if policy else request['timeout']
            run, grant = await self.authority(c, context=context)
            if grant and grant.get('job_override_seconds') is not None:
                selected = grant['job_override_seconds']
            if policy and policy.owner_override is not None and (not grant or policy.owner_override != grant.get('job_override_seconds')):
                raise WorkerUnavailable('owner override requires reviewed owner grant')
            if _parent:
                selected = next_tier(_parent['selected_seconds'])
            try:
                authorize(selected, grant, explicit=bool(policy) or bool(_parent), continuation=bool(_parent), force_custom=bool(policy and policy.custom_limit and not _parent))
            except ExecutionError as exc:
                raise WorkerUnavailable(exc.code) from exc
            scope = run['request'].get('job_coverage') if run else None
            if run and run.get('topology')=='workflow/v1': await self.coverage_request(c,run,request)
            if scope and not permits_job(scope, await self.coverage_request(c, run, request), scope['profiles'].get(request['profile'])):
                raise WorkerUnavailable('job outside reviewed coverage')
            if run:
                account = await self.account(c, run)
                queued = await c.fetchval("SELECT count(*) FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome='queued'", self.lane, context.owner, context.run_id)
                if account['job_seconds_budget'] is not None and account['seconds_used'] + account['seconds_held'] >= account['job_seconds_budget']:
                    raise WorkerUnavailable('job_time_budget_exhausted')
                if account['dispatched'] + queued >= account['max_job_attempts']:
                    raise WorkerUnavailable('job_attempt_budget_exhausted')
            elif selected > 3600 or (policy and selected not in (900,1800,3600)):
                raise WorkerUnavailable('job_grant_required')
            await c.execute('INSERT INTO worker_callers(lane,owner,principal,caller_group,boundary) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',
                            self.lane, context.owner, context.principal, context.group, context.boundary)
            row = await c.fetchrow('INSERT INTO worker_job_attempts(lane,owner,principal,caller_group,execution_id,logical_job_id,run_id,run_generation,request,admission_digest,selected_seconds,boundary) '
                'VALUES($1,$2,$3,$4,$5,$5,$6,$7,$8::jsonb,$9,$10,$11) RETURNING *',
                self.lane, context.owner, context.principal, context.group, execution_id, context.run_id, context.run_generation, canonical(request), admission_digest, selected, context.boundary)
            await c.execute('UPDATE worker_job_attempts SET proposed_seconds=$3,rationale=$4,owner_override=$5,auto_escalate=$6,explicit_policy=$7,grant_id=$8,grant_revision=$9,parent_execution_id=$10,logical_job_id=$11,timeout_episode=$12,reconciliation_evidence=$13::jsonb WHERE lane=$1 AND execution_id=$2',
                self.lane, execution_id, policy.proposed_seconds if policy else request['timeout'],
                policy.rationale if policy else 'legacy bounded tool request', grant.get('job_override_seconds') if grant else None,
                (policy.auto_escalate if policy else True) and (grant.get('job_auto_escalate', True) if grant else True),
                bool(policy) or bool(_parent), grant['id'] if grant else None, grant['revision'] if grant else None,
                _parent['execution_id'] if _parent else None, _parent['logical_job_id'] if _parent else execution_id,
                _parent['timeout_episode'] + 1 if _parent else 0, canonical(_proof) if _proof else None)
            await c.execute('UPDATE worker_job_attempts SET custom_limit=$3 WHERE lane=$1 AND execution_id=$2',self.lane,execution_id,bool(policy and policy.custom_limit and not _parent))
            await c.execute('INSERT INTO worker_queue(lane,owner,execution_id) VALUES($1,$2,$3)', self.lane, context.owner, execution_id)
            await self.event(c, row, 'queued', {'state': 'waiting_for_lane_worker'})
            return decoded(await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2', self.lane, execution_id))

    async def get(self, execution_id, *, connection=None):
        async with self.connection(connection) as c:
            return decoded(await c.fetchrow('''SELECT a.*,g.paused_unknown,g.paused_timeout FROM worker_job_attempts a
                JOIN worker_callers g ON (g.lane,g.owner,g.principal,g.caller_group)=(a.lane,a.owner,a.principal,a.caller_group)
                WHERE a.lane=$1 AND a.execution_id=$2''', self.lane, execution_id))

    async def slot(self, c):
        return dict(await c.fetchrow('SELECT * FROM worker_lane_slots WHERE lane=$1', self.lane))

    async def takeover(self, c, controller, minimum=1):
        return dict(await c.fetchrow('UPDATE worker_lane_slots SET generation=greatest(generation+1,$3),controller_session=$2,cleanup=\'unknown\' WHERE lane=$1 RETURNING *', self.lane, controller, minimum))

    async def barrier(self, c, slot, receipt):
        if receipt.get('type') != 'idle_barrier' or receipt.get('cleanup') != 'idle_proved' or receipt.get('lane_generation') != slot['generation'] or receipt.get('controller_session') != slot['controller_session']:
            raise WorkerUnavailable('invalid idle barrier')
        await c.execute("UPDATE worker_lane_slots SET cleanup='idle_proved',daemon_epoch=$3 WHERE lane=$1 AND generation=$2", self.lane, slot['generation'], receipt['daemon_epoch'])

    async def select(self, c, slot, profiles, *, max_timeout=3600):
        async with c.transaction():
            await self.grant_fence(c)
            await self.policy_fence(c)
            await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
            physical = await c.fetchrow('SELECT * FROM worker_lane_slots WHERE lane=$1 FOR UPDATE', self.lane)
            if (physical['generation'], physical['controller_session']) != (slot['generation'], slot['controller_session']):
                raise WorkerUnavailable('stale dispatch controller')
            if physical['active_execution'] or physical['cleanup'] != 'idle_proved':
                return None
            row = decoded(await c.fetchrow('''SELECT a.* FROM worker_queue q JOIN worker_job_attempts a
                ON (a.lane,a.execution_id)=(q.lane,q.execution_id) JOIN worker_callers g
                ON (g.lane,g.owner,g.principal,g.caller_group)=(a.lane,a.owner,a.principal,a.caller_group)
                WHERE q.lane=$1 AND a.outcome='queued' AND (a.cancel_requested OR
                (NOT g.paused_unknown AND (g.paused_timeout IS NULL OR a.parent_execution_id=g.paused_timeout)))
                ORDER BY g.last_served,q.ticket LIMIT 1 FOR UPDATE OF a''', self.lane))
            if not row:
                return None
            if row['cancel_requested'] or not await self.policy_allows(c, row['owner'], row['request']):
                await self._settle(c, row, dict(outcome='cancelled', cleanup='idle_proved'), slot, check_identity=False)
                return None
            try:
                run, grant = await self.authority(c, row=row)
                authorize(row['selected_seconds'], grant, explicit=row['explicit_policy'], continuation=bool(row['parent_execution_id']), force_custom=row['custom_limit'])
                if (row['grant_id'], row['grant_revision']) != ((grant['id'], grant['revision']) if grant else (None,None)):
                    raise WorkerUnavailable('pinned_job_grant_changed')
                account = await self.account(c, run) if run else None
                if row['parent_execution_id'] and not self.verified_workspace(row,row['reconciliation_evidence']['workspace_digest']):
                    raise WorkerUnavailable('workspace changed after reconciliation')
                if account and account['dispatched'] >= account['max_job_attempts']:
                    raise ExecutionError('job_attempt_budget_exhausted', 409)
                scope = run['request'].get('job_coverage') if run else None
                if run and run.get('topology')=='workflow/v1': await self.coverage_request(c,run,row['request'])
                if scope and not permits_job(scope, await self.coverage_request(c, run, row['request']), profiles.get(row['request']['profile'])):
                    raise ExecutionError('job_profile_or_scope_changed', 409)
                effective, limiting = effective_limit(row['selected_seconds'],
                    run_wall=run['deadline'].timestamp()-self.clock() if run and run['deadline'] else None,
                    time_budget=account['job_seconds_budget']-account['seconds_used']-account['seconds_held'] if account and account['job_seconds_budget'] is not None else None,
                    owner_ceiling=grant.get('job_seconds_ceiling') if grant else None, installed=max_timeout)
            except (WorkerUnavailable, ExecutionError) as exc:
                await self.pause(c, row, str(exc))
                await self._settle(c, row, dict(outcome='cancelled',cleanup='idle_proved',reason=str(exc)),slot,check_identity=False)
                return None
            r = row['request']
            if r['profile'] not in profiles:
                await self._settle(c,row,dict(type='result',outcome='failed',cleanup='idle_proved',
                    returncode=-1,killed=False,reason='installed_profile_unavailable'),slot,check_identity=False)
                return None
            now = self.clock()
            wire = {k: v for k, v in r.items() if k not in {'job_id', 'expires', 'version'}}
            wire['timeout'] = effective
            wire.update(version=2, op='submit', execution_id=row['execution_id'], logical_job_id=row['logical_job_id'],
                        lane_generation=slot['generation'], controller_session=slot['controller_session'],
                        profile_revision=profiles[r['profile']], not_after=now + effective, submit_expires_at=now + min(20, effective))
            if row['run_id'] and run['deadline']:
                wire['not_after'] = min(wire['not_after'],run['deadline'].timestamp())
                wire['submit_expires_at'] = min(wire['submit_expires_at'],wire['not_after'])
            wire['request_digest'] = request_digest(wire)
            try:
                validate_v2(wire,now=now,max_timeout=max_timeout)
            except ValueError as exc:
                await self._settle(c,row,dict(type='result',outcome='failed',cleanup='idle_proved',
                    returncode=-1,killed=False,reason=str(exc)),slot,check_identity=False)
                return None
            await c.execute("UPDATE worker_job_attempts SET request=$3::jsonb,request_digest=$4,lane_generation=$5,controller_session=$6,daemon_epoch=$7,effective_seconds=$8,outcome='dispatch_intent',cleanup='pending',dispatched_at=clock_timestamp() WHERE lane=$1 AND execution_id=$2",
                self.lane, row['execution_id'], canonical(wire), wire['request_digest'], slot['generation'], slot['controller_session'], slot['daemon_epoch'], effective)
            await c.execute('UPDATE worker_job_attempts SET limiting_reason=$3 WHERE lane=$1 AND execution_id=$2', self.lane, row['execution_id'], limiting)
            if run:
                await c.execute('UPDATE run_job_accounts SET dispatched=dispatched+1,held=held+1,seconds_held=seconds_held+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3', self.lane, row['owner'], row['run_id'], effective)
            await c.execute('INSERT INTO worker_job_dedup VALUES($1,$2,$3,$4)', self.lane, row['owner'], row['execution_id'], wire['request_digest'])
            await c.execute("UPDATE worker_lane_slots SET active_execution=$2,cleanup='pending' WHERE lane=$1", self.lane, row['execution_id'])
            await c.execute('UPDATE worker_callers SET last_served=(SELECT coalesce(max(last_served),0)+1 FROM worker_callers WHERE lane=$1) WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4',
                            self.lane, row['owner'], row['principal'], row['caller_group'])
            await self.event(c, row, 'dispatch_intent', {'request_digest': wire['request_digest'], 'generation': slot['generation'],
                'selected_seconds':row['selected_seconds'],'effective_seconds':effective,'limiting_reason':limiting})
            return decoded(await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2', self.lane, row['execution_id']))

    async def policy_fence(self, c):
        if os.environ.get('BOB_SETTINGS_ID'):
            await self.grant_fence(c)

    async def policy_allows(self, c, owner, request):
        if not os.environ.get('BOB_SETTINGS_ID'):
            return True
        from .policy import current
        # Legacy unvouched lane-service jobs still fall under the configured
        # owner's ceiling, never under an arbitrary context owner string.
        value = await current(c, self.lane, os.environ.get('BOB_DEFAULT_OWNER', owner))
        if value['policy'] is None or value['policy'].get('posture') == 'inherit':
            return True
        # CLI arguments are not a proven path/effect inventory. Once an editable
        # policy exists, opaque shell/Python/CLI work requires explicit review.
        # A profile allowlist cannot establish filesystem containment.
        return False

    async def cancel(self, execution_id, context):
        async with self.pool.acquire() as c, c.transaction():
            await self.grant_fence(c)
            await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
            row = decoded(await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2 FOR UPDATE', self.lane, execution_id))
            if not row:
                return  # Cancelled admission transaction rolled back; nothing owns IO.
            if (row['owner'], row['principal'], row['caller_group'], row['boundary'], row['run_id'], row['run_generation']) != (context.owner, context.principal, context.group, context.boundary, context.run_id, context.run_generation):
                raise WorkerUnavailable('stale cancellation owner')
            if row['outcome'] not in TERMINAL:
                await c.execute('UPDATE worker_job_attempts SET cancel_requested=TRUE WHERE lane=$1 AND execution_id=$2', self.lane, execution_id)
                await self.event(c, row, 'cancellation_intent')
                if row['outcome'] == 'queued':
                    # No dispatch intent exists, so this attempt owns no child.
                    await self._settle(c, row, dict(outcome='cancelled', cleanup='idle_proved'), None, check_identity=False)

    async def settle(self, c, row, receipt, slot, *, recovered=False):
        async with c.transaction():
            await self.grant_fence(c)
            await c.execute('SELECT pg_advisory_xact_lock($1)', self.lock)
            current = await c.fetchval('SELECT generation FROM worker_lane_slots WHERE lane=$1 FOR UPDATE', self.lane)
            if current != slot['generation']:
                raise WorkerUnavailable('stale settlement controller')
            await self._settle(c, row, receipt, slot, check_identity=True, recovered=recovered)

    async def _settle(self, c, row, receipt, slot, *, check_identity, recovered=False):
        if check_identity:
            for key in ('execution_id', 'request_digest', 'lane_generation', 'controller_session'):
                if receipt.get(key) != row[key]:
                    raise WorkerUnavailable('terminal identity conflict')
            if not recovered and receipt.get('daemon_epoch') != row['daemon_epoch']:
                raise WorkerUnavailable('terminal epoch conflict')
        if receipt.get('cleanup') != 'idle_proved' or receipt.get('outcome') not in TERMINAL:
            raise WorkerUnavailable('cleanup/outcome not established')
        current = decoded(await c.fetchrow('SELECT * FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2 FOR UPDATE', self.lane, row['execution_id']))
        if current['outcome'] in TERMINAL:
            return
        cancelled = await c.fetchval('SELECT cancel_requested FROM worker_job_attempts WHERE lane=$1 AND execution_id=$2', self.lane, row['execution_id'])
        outcome = receipt['outcome']
        if cancelled and outcome != 'unknown':
            outcome = 'cancelled'
        # Only run-owned jobs use the reconcile-then-next-tier ladder. A known
        # standalone timeout with idle proof must not strand its caller group.
        run_timeout = outcome == 'timed_out' and current['run_id'] is not None
        reconciliation = 'unknown' if outcome == 'unknown' else 'pending' if run_timeout else 'established'
        elapsed, cleanup_seconds = None, None
        if current['dispatched_at'] and current['run_id']:
            reserved = current['effective_seconds']
            if outcome == 'unknown':
                await c.execute('UPDATE run_job_accounts SET unknown=unknown+1 WHERE lane=$1 AND owner=$2 AND run_id=$3',self.lane,current['owner'],current['run_id'])
            else:
                elapsed = receipt.get('execution_seconds', reserved)
                cleanup_seconds = receipt.get('cleanup_seconds', 0)
                if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in (elapsed,cleanup_seconds)) or elapsed > reserved + 30:
                    raise WorkerUnavailable('invalid measured job time')
                await c.execute('UPDATE run_job_accounts SET held=held-1,completed=completed+1,seconds_held=greatest(0,seconds_held-$4),seconds_used=seconds_used+$5,cleanup_seconds=cleanup_seconds+$6 WHERE lane=$1 AND owner=$2 AND run_id=$3',
                                self.lane,current['owner'],current['run_id'],reserved,elapsed,cleanup_seconds)
        await c.execute('UPDATE worker_job_attempts SET execution_seconds=$3,cleanup_seconds=$4 WHERE lane=$1 AND execution_id=$2', self.lane,row['execution_id'],elapsed,cleanup_seconds)
        await c.execute("UPDATE worker_job_attempts SET outcome=$3,cleanup='idle_proved',reconciliation=$4,receipt=$5::jsonb,finished_at=clock_timestamp() WHERE lane=$1 AND execution_id=$2",
                        self.lane, row['execution_id'], outcome, reconciliation, canonical(receipt))
        await c.execute('DELETE FROM worker_queue WHERE lane=$1 AND execution_id=$2', self.lane, row['execution_id'])
        await c.execute("UPDATE worker_lane_slots SET active_execution=NULL,cleanup='idle_proved' WHERE lane=$1 AND active_execution=$2", self.lane, row['execution_id'])
        if row['run_id']:
            await c.execute("UPDATE agent_runs SET cleanup_pending=EXISTS(SELECT 1 FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome IN ('dispatch_intent','running')) OR EXISTS(SELECT 1 FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='unknown') WHERE lane=$1 AND owner=$2 AND id=$3",self.lane,row['owner'],row['run_id'])
        await self.event(c, row, 'reconciled' if recovered else 'terminal', {'outcome': outcome, 'cleanup': 'idle_proved'})
        if outcome not in {'timed_out','unknown'} and current['parent_execution_id']:
            await c.execute('UPDATE worker_callers SET paused_timeout=NULL WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4 AND paused_timeout=$5',self.lane,row['owner'],row['principal'],row['caller_group'],current['parent_execution_id'])
        if run_timeout:
            await c.execute('UPDATE worker_callers SET paused_timeout=$5 WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4', self.lane,row['owner'],row['principal'],row['caller_group'],row['execution_id'])
            await self.event(c,row,'timeout_reconciliation_required',{'logical_job_id':current['logical_job_id'],'timeout_episode':current['timeout_episode']})
            if current['limiting_reason'] != 'selected_limit' or not current['auto_escalate']:
                await self.pause(c,row,current['limiting_reason'] if current['limiting_reason'] != 'selected_limit' else 'job_escalation_disabled')
        if outcome == 'unknown':
            await c.execute('UPDATE worker_callers SET paused_unknown=TRUE WHERE lane=$1 AND owner=$2 AND principal=$3 AND caller_group=$4',
                            self.lane, row['owner'], row['principal'], row['caller_group'])
        if outcome == 'unknown' and row['run_id']:
            await c.execute("UPDATE agent_runs SET status='paused',reason='worker_outcome_unknown',revision=revision+1 WHERE lane=$1 AND owner=$2 AND id=$3", self.lane, row['owner'], row['run_id'])

    async def visible(self, execution_id, *, connection=None):
        async with self.connection(connection) as c:
            row = await self.get(execution_id,connection=c)
            slot = await self.slot(c)
            position = await c.fetchval('SELECT count(*) FROM worker_queue WHERE lane=$1 AND ticket <= (SELECT ticket FROM worker_queue WHERE lane=$1 AND execution_id=$2)', self.lane, execution_id)
            active = await self.get(slot['active_execution'],connection=c) if slot['active_execution'] else None
            elapsed = max(0, self.clock() - active['dispatched_at'].timestamp()) if active and active['dispatched_at'] else None
            state = 'cancelling' if row['cancel_requested'] and row['outcome'] not in TERMINAL else 'waiting_for_lane_worker' if row['outcome'] == 'queued' else row['outcome']
            return dict(execution_id=execution_id, state=state,
                        queued_at=row['queued_at'].isoformat(), queue_position=position,
                        queue_position_observed_at=self.clock(), selected_tier=row['selected_seconds'], effective_seconds=row['effective_seconds'],
                        active_job_elapsed=elapsed,
                        proposed_seconds=row['proposed_seconds'], rationale=row['rationale'], owner_override=row['owner_override'],
                        limiting_reason=row['limiting_reason'], logical_job_id=row['logical_job_id'],
                        parent_execution_id=row['parent_execution_id'], timeout_episode=row['timeout_episode'],
                        reconciliation=row['reconciliation'], grant_id=row['grant_id'], grant_revision=row['grant_revision'],
                        blocked_by_timeout_reconciliation=bool(row['paused_timeout']),
                        blocked_by_unknown_outcome=row['paused_unknown'],
                        blocked_by_cleanup=slot['cleanup'] != 'idle_proved',
                        blocked_by_recovery=slot['cleanup'] == 'unknown')
