"""Workflow persistence on Store's transactions, receipts and run ledger.

wf_* methods are the WB2 persistence seam. They accept an existing transaction;
none dispatch a model, claim a workspace or execute a worker job.
"""
import json
import uuid
from datetime import datetime
from .models import ExecutionError, canonical, digest


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def timestamp(value):
    # asyncpg's timestamptz codec takes datetime, even with an explicit SQL cast.
    return datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value


class WorkflowStore:
    async def wf_clock(self,c):
        return await c.fetchval('SELECT clock_timestamp()')

    async def wf_lock(self, c, auth, kind, identifier):
        await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',
                        int(digest([auth.lane, auth.owner, 'workflow', kind, identifier])[:15], 16))

    async def wf_get(self, c, auth, kind, identifier, revision=None):
        if kind == 'definition':
            row = await c.fetchrow('''SELECT v.*,h.deleted,h.revision AS head_revision,
                (SELECT min(first.created_at) FROM workflow_versions first WHERE first.lane=v.lane
                 AND first.owner=v.owner AND first.id=v.id) AS first_created_at FROM workflow_versions v
                JOIN workflow_heads h USING(lane,owner,id) WHERE v.lane=$1 AND v.owner=$2 AND v.id=$3
                AND v.revision=COALESCE($4::bigint,h.revision) FOR UPDATE OF h''', auth.lane, auth.owner, identifier, revision)
            if row and (row['origin']=='premade' or auth.permits(row)):
                return {'id':row['id'], 'revision':row['revision'], 'digest':row['digest'], 'schema':'workflow/v1',
                        'definition':decoded(row['definition']), 'origin':row['origin'], 'read_only':row['read_only'],
                        'deleted':row['deleted'], 'created_at':row['first_created_at'].isoformat(),
                        'updated_at':row['created_at'].isoformat(), 'boundary':row['boundary']}
        elif kind == 'proposal':
            row = await c.fetchrow('SELECT * FROM agent_admission_intents WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE',
                                   auth.lane, auth.owner, identifier)
            if row and auth.permits(row) and row['workflow_coverage'] is not None:
                return decoded(row['workflow_coverage'])
        elif kind == 'run':
            row = await c.fetchrow('SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE',
                                   auth.lane, auth.owner, identifier)
            if row and auth.permits(row) and row['topology'] == 'workflow/v1':
                record = decoded(row['workflow_state'])
                if not record['run'].get('proposal_id'):
                    # Pre-BU14 JSON snapshots omit this link. The original grant
                    # already stores it durably; scope the lookup like the run.
                    record['run']['proposal_id'] = await c.fetchval('''SELECT proposal_id FROM agent_execution_grants
                        WHERE lane=$1 AND owner=$2 AND id=$3 AND run_id=$4 AND matcher_version=4''',
                        auth.lane, auth.owner, row['selected_grant_id'], identifier)
                return record
        return None

    async def wf_list(self, c, auth, kind, cursor, limit):
        table = 'workflow_heads' if kind == 'definition' else 'agent_runs'
        extra = 'AND NOT deleted' if kind == 'definition' else "AND topology='workflow/v1'"
        rows = await c.fetch(f'''SELECT id FROM {table} WHERE lane=$1 AND owner=$2 AND id>$3 {extra}
            AND ($4::text IS NULL OR boundary=$4) ORDER BY id LIMIT $5''',
            auth.lane, auth.owner, cursor, auth.boundary if auth.token_type == 'agent' else None, limit)
        return [await self.wf_get(c, auth, kind, r['id']) for r in rows]

    async def wf_definition(self, c, auth, record):
        await c.execute('''INSERT INTO workflow_heads(lane,owner,id,revision,deleted,boundary) VALUES($1,$2,$3,$4,$5,$6)
            ON CONFLICT(lane,owner,id) DO UPDATE SET revision=EXCLUDED.revision,deleted=EXCLUDED.deleted''',
            auth.lane, auth.owner, record['id'], record['revision'], record['deleted'], record['boundary'])
        if not record['deleted']:
            await c.execute('''INSERT INTO workflow_versions(lane,owner,id,revision,definition,digest,origin,read_only,boundary,created_at)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10::timestamptz) ON CONFLICT DO NOTHING''',
                auth.lane, auth.owner, record['id'], record['revision'], canonical(record['definition']), record['digest'],
                record['origin'], record['read_only'], record['boundary'], timestamp(record['updated_at']))
        else:
            # Tombstones have their own immutable version; old pinned versions survive.
            copy = {**record, 'deleted':False}
            await c.execute('''INSERT INTO workflow_versions(lane,owner,id,revision,definition,digest,origin,read_only,boundary,created_at)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10::timestamptz)''',
                auth.lane, auth.owner, copy['id'], copy['revision'], canonical(copy['definition']), copy['digest'],
                copy['origin'], copy['read_only'], copy['boundary'], timestamp(copy['updated_at']))

    async def wf_proposal(self, c, auth, record, *, new=False):
        p = record['proposal']
        if new:
            await c.execute('''INSERT INTO agent_admission_intents(lane,owner,id,run_id,boundary,authority,request,snapshot,
                proposal_digest,workflow_coverage,coverage_digest) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10::jsonb,$9)''',
                auth.lane, auth.owner, p['id'], p['planned_run_id'], auth.boundary, canonical(auth.as_dict()),
                canonical(record['args']), canonical(p['coverage']), p['digest'], canonical(record))
        else:
            await c.execute('''UPDATE agent_admission_intents SET workflow_coverage=$4::jsonb,consumed=$5,
                grant_id=$6,grant_revision=CASE WHEN $6::text IS NULL THEN NULL ELSE 1 END
                WHERE lane=$1 AND owner=$2 AND id=$3''', auth.lane, auth.owner, p['id'], canonical(record),
                p['state']=='granted',record.get('grant_id'))

    async def wf_start(self, c, auth, record, proposal):
        """All inserts and proposal consumption share the caller's transaction."""
        w, coverage = record['run'], record['coverage']
        key = (auth.lane, auth.owner, w['id'])
        anchor = 'workflow-' + w['id']
        team = {'topology':'workflow/v1', 'definition':coverage['definition'], 'bindings':coverage['bindings']}
        # Internal FK anchor, never exposed through the text-team catalog.
        await c.execute('INSERT INTO agent_team_heads VALUES($1,$2,$3,1)', auth.lane, auth.owner, anchor)
        await c.execute('''INSERT INTO agent_team_versions(lane,owner,id,revision,actor,boundary,definition)
            VALUES($1,$2,$3,1,$4,$5,$6::jsonb)''', auth.lane, auth.owner, anchor, auth.actor, record['boundary'], canonical(team))
        request = {**proposal['args'], 'budget':coverage['budget'], 'project_id':coverage['project_id']}
        snapshot = {'team':team, 'models':coverage['bindings'], 'workflow_coverage':coverage,
                    'coverage_digest':digest(coverage), 'resolved':record['resolved']}
        await c.execute('''INSERT INTO agent_runs(lane,owner,id,actor,boundary,run_authority,team,team_revision,snapshot,request,status,
            topology,workflow_id,workflow_revision,workflow_state,grant_expires_at,selected_grant_id,selected_grant_revision,initial_budget)
            VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,1,$8::jsonb,$9::jsonb,'accepted','workflow/v1',$10,$11,$12::jsonb,$13::timestamptz,$14,1,$15::jsonb)''',
            *key, auth.actor, record['boundary'], canonical(record['initiator']), anchor, canonical(snapshot), canonical(request),
            (w['definition_ref'] or {}).get('id'), (w['definition_ref'] or {}).get('revision'), canonical(record),
            timestamp(w['grant']['expires_at']), w['grant']['id'], canonical(coverage['budget']))
        await c.execute('''INSERT INTO agent_execution_grants(lane,owner,id,run_id,project_id,authority_boundary,task_digest,
            team_digest,models_digest,call_ceiling,expires_at,matcher_version,coverage_kind,proposal_id,credential_epoch,
            workflow_coverage,coverage_digest,allow_long_runs,auto_escalate,job_auto_escalate)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::timestamptz,4,'task',$12,$13,$14::jsonb,$15,TRUE,FALSE,FALSE)''',
            auth.lane, auth.owner, w['grant']['id'], w['id'], w['project_id'], record['boundary'], digest(request), digest(team),
            digest(coverage['bindings']), coverage['budget']['calls'], timestamp(w['grant']['expires_at']), proposal['proposal']['id'],
            auth.credential_epoch, canonical(coverage), digest(coverage))
        grant_record = {'id':w['grant']['id'],'revision':1,'run_id':w['id'],'matcher_version':4,
                        'workflow_coverage':coverage,'coverage_digest':digest(coverage),
                        'call_ceiling':coverage['budget']['calls'],'expires_at':w['grant']['expires_at'],
                        'authority_boundary':record['boundary'],'credential_epoch':auth.credential_epoch,
                        'auto_escalate':False,'job_auto_escalate':False}
        await c.execute('''INSERT INTO agent_grant_versions(lane,owner,id,revision,record,actor,issuer_authority)
            VALUES($1,$2,$3,1,$4::jsonb,$5,$6::jsonb)''', auth.lane, auth.owner, w['grant']['id'], canonical(grant_record),
            auth.actor, canonical(auth.as_dict()))
        calls = coverage['budget']['calls']
        await c.execute('INSERT INTO agent_budget_accounts(lane,owner,run_id,call_limit) VALUES($1,$2,$3,$4)', *key, calls)
        await c.execute("INSERT INTO agent_budget_allocations(lane,owner,run_id,id,allowance) VALUES($1,$2,$3,'root',$4)", *key, calls)
        await c.execute('''INSERT INTO run_job_accounts(lane,owner,run_id,max_job_attempts,job_seconds_budget)
            VALUES($1,$2,$3,$4,$5)''', *key, coverage['budget']['max_job_attempts'], coverage['budget']['job_seconds'])
        await self._admit_pools(c, auth, request, w['id'], calls)
        for s in w['stages']:
            await c.execute('''INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,state,spec,checkpoint)
                VALUES($1,$2,$3,$4,$5,'pending',$6::jsonb,'{}')''', *key, s['ordinal'], s['agent_alias'] or s['kind'],
                canonical({'stage_key':s['key'], 'kind':s['kind'], 'parent_key':None, 'unit':None, 'iteration':0}))

    async def wf_run(self, c, auth, record):
        from .workflows import TERMINAL
        w = record['run']
        await c.execute('''UPDATE agent_runs SET workflow_state=$4::jsonb,status=$5,reason=$6,revision=$7,event_seq=$8,
            updated_at=$9::timestamptz,terminal_outcome=$10,active_elapsed_ms=$11 WHERE lane=$1 AND owner=$2 AND id=$3''',
            auth.lane, auth.owner, w['id'], canonical(record), w['status'], w['reason'], w['revision'], w['event_seq'],
            timestamp(w['updated_at']), w['status'] if w['status'] in TERMINAL else None,
            int(w['budget']['active_seconds']*1000))

    async def wf_grant_active(self,c,auth,identifier):
        return await c.fetchval('''SELECT NOT revoked AND expires_at>clock_timestamp() FROM agent_execution_grants
            WHERE lane=$1 AND owner=$2 AND id=$3 AND matcher_version=4''',auth.lane,auth.owner,identifier)

    async def wf_event(self, c, auth, event):
        await c.execute('''INSERT INTO agent_run_events(lane,owner,run_id,seq,kind,detail,created_at)
            VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::timestamptz)''', auth.lane, auth.owner, event['run_id'], event['seq'],
            event['kind'], canonical(event), timestamp(event['at']))

    async def wf_events(self, c, auth, run_id, after, limit):
        rows = await c.fetch('''SELECT detail FROM agent_run_events WHERE lane=$1 AND owner=$2 AND run_id=$3
            AND seq>$4 ORDER BY seq LIMIT $5''', auth.lane, auth.owner, run_id, after, limit)
        return [decoded(r['detail']) for r in rows]

    async def wf_stop(self, c, auth, record, *, release_holds=True):
        w = record['run']; keys = (auth.lane, auth.owner, w['id'])
        await c.execute('UPDATE agent_execution_grants SET revoked=TRUE WHERE lane=$1 AND owner=$2 AND run_id=$3', *keys)
        await c.execute("UPDATE workflow_waits SET state='expired',revision=revision+1 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='open'", *keys)
        for stage in w['stages']:
            await self.wf_stage(c,auth,w['id'],stage)
        if release_holds: await self._release_holds(c, keys)

    async def wf_request_stop(self,c,auth,record):
        keys=(auth.lane,auth.owner,record['run']['id'])
        await c.execute('UPDATE agent_execution_grants SET revoked=TRUE WHERE lane=$1 AND owner=$2 AND run_id=$3',*keys)
        await c.execute('''UPDATE agent_runs SET authorization_revoked=TRUE,cleanup_pending=TRUE
            WHERE lane=$1 AND owner=$2 AND id=$3''',*keys)
        await c.execute('''UPDATE worker_job_attempts SET cancel_requested=TRUE WHERE lane=$1 AND owner=$2 AND run_id=$3
            AND outcome IN ('queued','dispatch_intent','running')''',*keys)
        await c.execute("UPDATE workflow_waits SET state='expired',revision=revision+1 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='open'",*keys)

    async def wf_stage(self,c,auth,run_id,stage):
        await c.execute('''INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,state,spec,checkpoint,conversation_id,request_id)
            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10)
            ON CONFLICT(lane,owner,run_id,ordinal) DO UPDATE SET state=EXCLUDED.state,
            checkpoint=EXCLUDED.checkpoint,conversation_id=EXCLUDED.conversation_id,request_id=EXCLUDED.request_id''',
            auth.lane,auth.owner,run_id,stage['ordinal'],stage.get('agent_alias') or stage['kind'],stage['state'],
            canonical({'stage_key':stage['key'],'kind':stage['kind']}),canonical(stage),
            uuid.UUID(stage['conversation_id']) if stage.get('conversation_id') else None,
            uuid.UUID(stage['request_id']) if stage.get('request_id') else None)

    async def wf_open_wait(self,c,auth,run_id,stage,wait):
        await c.execute('''INSERT INTO workflow_waits(lane,owner,id,run_id,ordinal,kind,record_id,checkpoint_digest,state,expires_at,view)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,'open',$9,$10::jsonb)''',auth.lane,auth.owner,wait['wait_id'],run_id,
            stage['ordinal'],wait['kind'],wait['record_id'],digest(stage),timestamp(wait['expires_at']),canonical(wait))

    async def wf_claim(self,c,row,controller):
        from .store import record
        keys=(row['lane'],row['owner'],row['id'])
        await c.execute('''UPDATE agent_runs SET generation=generation+1,controller=$4,
            lease_until=clock_timestamp()+interval '60 seconds' WHERE lane=$1 AND owner=$2 AND id=$3''',*keys,controller)
        return record(await c.fetchrow('SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3',*keys))

    async def wf_release(self,c,row):
        await c.execute('''UPDATE agent_runs SET controller=NULL,lease_until=NULL,updated_at=clock_timestamp()
            WHERE lane=$1 AND owner=$2 AND id=$3 AND generation=$4 AND controller=$5''',
            row['lane'],row['owner'],row['id'],row['generation'],row['controller'])

    async def wf_charge(self,c,row,stage,binding,prompt,record):
        """Charge one model request under the existing root and project holds."""
        keys=(row['lane'],row['owner'],row['id'])
        await self._lock_pools(c,keys)
        chain=await self._chain(c,keys,'root')
        root=chain[0]
        if root['allowance']-root['consumed']-root['reserved']-root['delegated']<1:
            raise ExecutionError('call_budget_exhausted',409)
        identifier=str(uuid.uuid4())
        await c.execute('''INSERT INTO agent_attempts(lane,owner,run_id,id,ordinal,generation,binding,input_digest,
            state,allocation_id,round_index,dispatched_at) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,'dispatched','root',$9,clock_timestamp())''',
            *keys,identifier,stage['ordinal'],row['generation'],canonical(binding),digest(prompt),stage.get('calls',0))
        await c.execute("UPDATE agent_budget_allocations SET consumed=consumed+1 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id='root'",*keys)
        await c.execute('UPDATE agent_budget_accounts SET consumed=consumed+1 WHERE lane=$1 AND owner=$2 AND run_id=$3',*keys)
        for hold in await c.fetch('SELECT * FROM agent_pool_holds WHERE lane=$1 AND owner=$2 AND run_id=$3',*keys):
            if hold['remaining']<1: raise ExecutionError('pool_shortfall',409)
            await c.execute('UPDATE agent_call_pools SET reserved=reserved-1,consumed=consumed+1 WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4',
                keys[0],keys[1],hold['kind'],hold['pool_id'])
            await c.execute('UPDATE agent_pool_holds SET remaining=remaining-1 WHERE lane=$1 AND owner=$2 AND run_id=$3 AND kind=$4 AND pool_id=$5',
                *keys,hold['kind'],hold['pool_id'])
        return identifier

    async def wf_model_failure(self,c,row,attempt,failure):
        await c.execute("UPDATE agent_attempts SET state='failed',failure_detail=$5::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4",
                        row['lane'],row['owner'],row['id'],attempt,
                        canonical({'code':failure.code,'detail':failure.sentence}))

    async def wf_model_result(self,c,row,attempt):
        await c.execute("UPDATE agent_attempts SET state='completed' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4",row['lane'],row['owner'],row['id'],attempt)

    async def wf_conversation(self,c,auth,record,stage,face):
        conversation=uuid.UUID(stage['conversation_id'])
        await c.execute('''INSERT INTO conversations(id,user_id,title,face_id,project_id)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING''',conversation,auth.owner,
            record['run']['name']+' / '+stage['key'],face,
            uuid.UUID(record['run']['project_id']) if record['run']['project_id'] else None)

    async def wf_history(self,c,auth,run_id,stage,role,content,segment):
        identifier=uuid.uuid5(uuid.NAMESPACE_URL,run_id+'/'+stage['key']+'/'+stage['request_id']+'/'+role+'/'+str(segment))
        await c.execute('''INSERT INTO messages(id,conversation_id,role,content,metadata)
            VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(id) DO NOTHING''',identifier,
            uuid.UUID(stage['conversation_id']),role,content,canonical({'kind':'workflow_stage','run_id':run_id,
            'stage_key':stage['key'],'request_id':stage['request_id']}))

    async def wf_writer(self,c,row,auth,record,stage):
        from .workflow_context import mutation_lock,writer_guard
        project=record['run']['project_id']
        if not project: return
        await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',mutation_lock(auth,project))
        await writer_guard(c,auth,project,run_id=row['id'],generation=row['generation'])
        if await c.fetchval('''SELECT EXISTS(SELECT 1 FROM builder_approvals WHERE lane=$1 AND owner=$2
            AND project_id=$3 AND execution_state IN ('running','unknown'))''',auth.lane,auth.owner,uuid.UUID(project)):
            raise ExecutionError('workspace_busy',409,'An earlier Builder operation has not proved cleanup in this workspace.')
        attachment=(record['resolved'].get('registration') or {}).get('attachment_id')
        if attachment and await c.fetchval('''SELECT EXISTS(SELECT 1 FROM work_publications p JOIN tool_jobs j
            ON (j.lane,j.owner,j.id)=(p.lane,p.owner,p.job_id) WHERE p.lane=$1 AND p.owner=$2
            AND j.attachment_id=$3 AND p.state IN ('intent','unknown'))''',auth.lane,auth.owner,attachment):
            raise ExecutionError('workspace_busy',409,'A publication is still using this workspace or requires reconciliation.')
        registration=record['coverage']['workspace']['registration_digest']
        await c.execute('''INSERT INTO workflow_workspace_leases(lane,owner,registration_digest,run_id,stage_key,generation,lease_until)
            VALUES($1,$2,$3,$4,$5,$6,clock_timestamp()+interval '60 seconds')
            ON CONFLICT(lane,owner,registration_digest) DO UPDATE SET stage_key=EXCLUDED.stage_key,
            generation=EXCLUDED.generation,lease_until=EXCLUDED.lease_until''',auth.lane,auth.owner,
            registration,row['id'],stage['key'],row['generation'])

    async def wf_cleanup(self,c,row,auth,record,*,release_workspace=True):
        keys=(auth.lane,auth.owner,row['id'])
        unresolved=await c.fetchval('''SELECT EXISTS(SELECT 1 FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3
            AND (outcome NOT IN ('succeeded','failed','timed_out','cancelled') OR cleanup IS DISTINCT FROM 'idle_proved'))''',*keys)
        if unresolved: return False
        await c.execute('''UPDATE builder_approvals SET execution_state='stopped',result=$4::jsonb
            WHERE lane=$1 AND owner=$2 AND execution_state IN ('running','unknown') AND conversation_id IN
            (SELECT conversation_id FROM builder_modes WHERE lane=$1 AND owner=$2 AND workflow_run_id=$3)''',
            *keys,canonical({'status':'stopped','cleanup':'idle_proved','end_turn':True,
                'detail':'The workflow worker is idle. Earlier partial effects must be inspected; this is not a success receipt.'}))
        if release_workspace:
            await c.execute('DELETE FROM workflow_workspace_leases WHERE lane=$1 AND owner=$2 AND run_id=$3',*keys)
        await c.execute('UPDATE agent_runs SET cleanup_pending=FALSE WHERE lane=$1 AND owner=$2 AND id=$3',*keys)
        return True

    async def wf_recovery_ready(self,c,auth,record):
        return await self.wf_cleanup(c,{'id':record['run']['id']},auth,record)

    async def wf_job_usage(self,c,auth,run_id):
        row=await c.fetchrow('SELECT seconds_used,dispatched FROM run_job_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3',auth.lane,auth.owner,run_id)
        return {'job_seconds':float(row['seconds_used']),'job_attempts':row['dispatched']} if row else {'job_seconds':0,'job_attempts':0}

    async def wf_end_session(self,c,auth,run_id,reason):
        from .models import Authority
        from .workflows import WorkflowService,TERMINAL
        from types import SimpleNamespace
        raw=await c.fetchval('SELECT workflow_state FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3 FOR UPDATE',auth.lane,auth.owner,run_id)
        if not raw: return
        record=decoded(raw); w=record['run']
        if w['status'] in TERMINAL: return
        w.update(status='stopping',reason=reason); w['grant']['active']=False
        w['needs_you']=[]
        original=Authority(**record['initiator'])
        await self.wf_request_stop(c,original,record)
        await WorkflowService(SimpleNamespace(store=self)).event(c,original,record,'run_state',{'status':'stopping','reason':reason})

    async def wf_interrupted_calls(self,c,row):
        keys=(row['lane'],row['owner'],row['id'])
        count=await c.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'",*keys)
        if count:
            await c.execute("UPDATE agent_attempts SET state='unknown' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'",*keys)
            await c.execute('UPDATE agent_budget_accounts SET unknown_calls=unknown_calls+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3',*keys,count)
        return count

    async def wf_message(self, c, auth, run_id, stage, identifier, text):
        await c.execute('INSERT INTO workflow_messages(lane,owner,id,run_id,ordinal,text) VALUES($1,$2,$3,$4,$5,$6)',
                        auth.lane, auth.owner, identifier, run_id, stage['ordinal'], text)

    async def wf_take_messages(self,c,auth,run_id,stage):
        rows=await c.fetch('''SELECT id,text FROM workflow_messages WHERE lane=$1 AND owner=$2 AND run_id=$3
            AND ordinal=$4 AND consumed_at IS NULL ORDER BY created_at,id FOR UPDATE''',auth.lane,auth.owner,run_id,stage['ordinal'])
        for row in rows:
            await c.execute('UPDATE workflow_messages SET consumed_at=clock_timestamp() WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,row['id'])
        return [dict(row) for row in rows]

    async def wf_wait(self, c, auth, run_id, wait, decision):
        result = await c.execute('''UPDATE workflow_waits SET state=$5,decision=$6::jsonb,revision=revision+1,
            view=$7::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4 AND state='open' ''',
            auth.lane, auth.owner, run_id, wait['wait_id'], wait['state'], canonical(decision), canonical(wait))
        if result != 'UPDATE 1':
            raise ExecutionError('revision_conflict', 409)

    async def wf_artifact(self, c, auth, run_id, identifier, content=None):
        if content is not None:
            import hashlib
            await c.execute('''INSERT INTO workflow_artifacts(lane,owner,run_id,id,digest,media_type,bytes)
                VALUES($1,$2,$3,$4,$5,'application/json',$6) ON CONFLICT DO NOTHING''',
                auth.lane, auth.owner, run_id, identifier, hashlib.sha256(content).hexdigest(), content)
            return
        row = await c.fetchrow('SELECT bytes,digest,media_type FROM workflow_artifacts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND id=$4',
                               auth.lane, auth.owner, run_id, identifier)
        return dict(row) if row else None

    async def wf_report(self, c, auth, record):
        """Deterministic report before delivery; no summary inference in WB1."""
        w = record['run']; report = w['report']; conversation = w['origin_conversation_id']
        # One origin message per run, including pause/resume and recovery.
        # Immutable artifacts retain every report revision.
        message = uuid.uuid5(uuid.NAMESPACE_URL, w['id'] + '/report')
        if conversation:
            await c.execute('''INSERT INTO messages(id,conversation_id,role,content,metadata)
                VALUES($1,$2,'assistant',$3,$4::jsonb) ON CONFLICT(id) DO UPDATE
                SET content=EXCLUDED.content,metadata=EXCLUDED.metadata''', message, uuid.UUID(conversation),
                report['summary'], canonical({'kind':'workflow_report', 'author':'workflow_controller', 'run_id':w['id'],
                                             'report_digest':report['digest'], 'artifact_id':report['artifact_id']}))
        await c.execute('''INSERT INTO workflow_report_outbox(lane,owner,run_id,report_digest,message_id,conversation_id,state)
            VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(lane,owner,run_id) DO UPDATE SET report_digest=EXCLUDED.report_digest,
            message_id=EXCLUDED.message_id,conversation_id=EXCLUDED.conversation_id,state=EXCLUDED.state''', auth.lane, auth.owner, w['id'], report['digest'],
            message, uuid.UUID(conversation) if conversation else None, 'delivered' if conversation else 'none')
