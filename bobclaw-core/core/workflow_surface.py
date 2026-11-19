"""Workflow presentation additions; execution remains in WorkflowService."""
import base64
from datetime import timedelta
import json
from core.execution.models import ExecutionError, only, wire_integers
from core.execution.workflows import WorkflowService, number, utc, identifier, capabilities as service_capabilities


def capabilities(storage_ready, runner_ready=False):
    return {**service_capabilities(storage_ready, runner_ready), 'workflows.proposals': {
        'version': 1, 'contract_versions': [1], 'implemented': True,
        'available': bool(storage_ready),
        'reason': None if storage_ready else 'execution_storage_unavailable',
        'owner_session_required': False, 'request_key_required': False,
        'include_expired': True}}


class WorkflowSurface(WorkflowService):
    async def proposal_record(self, c, auth, id):
        record = await super().proposal_record(c, auth, id)
        if record['args'].get('origin_conversation_id'):
            record['proposal']['origin_conversation_id'] = record['args']['origin_conversation_id']
        return record

    async def _propose(self, c, auth, args):
        result = await super()._propose(c, auth, args)
        record = await self.store.wf_get(c, auth, 'proposal', result['proposal']['id'])
        # 'via' says how the proposal arrived, from authenticated context only.
        record['proposal']['proposed_by'] = {'actor': auth.actor, 'token_type': auth.token_type,
            'via': 'assistant' if self.assistant_origin is not None else 'agent' if auth.token_type == 'agent' else 'owner'}
        await self.store.wf_proposal(c, auth, record)
        return {**result, 'proposal': record['proposal']}

    async def command(self, auth, operation, args):
        if operation in {'workflows.message', 'workflows.stop'} and self.assistant_origin is not None:
            # Check before the mutation replay path too: a receipt from another
            # conversation is not authority to observe or steer that run.
            async with self.store.transaction() as c:
                record = await self.run_record(c, auth, args.get('run_id'))
                if record['run']['origin_conversation_id'] != self.assistant_origin:
                    raise ExecutionError('not_found', 404)
        if operation != 'workflows.proposals':
            return await super().command(auth, operation, args)
        only(args, {'contract_version', 'cursor', 'limit', 'include_expired'})
        if type(args.get('contract_version')) is not int or args['contract_version'] != 1:
            raise ExecutionError('workflow_contract_required')
        if (auth.lane != self.store.lane or not auth.owner or not auth.actor
                or auth.token_type not in {'human', 'portal', 'agent'}):
            raise ExecutionError('not_found', 404)
        if auth.session_kind or auth.audience:
            raise ExecutionError('owner_audience_denied', 403)
        cursor = args.get('cursor', '')
        if not isinstance(cursor, str):
            raise ExecutionError('invalid_cursor')
        limit = number(args.get('limit', 20), 1, 50)
        if type(args.get('include_expired', False)) is not bool:
            raise ExecutionError('invalid_args',422)
        if args.get('include_expired'):
            return await self.proposal_history(auth, cursor, limit)
        # Existing durable admission records are the owner's Needs you source.
        # Scope and expiry are checked again through the normal service lookup.
        items = []
        async with self.store.transaction() as c:
            while len(items) <= limit:
                rows = await c.fetch('''SELECT id FROM agent_admission_intents
                    WHERE lane=$1 AND owner=$2 AND id>$3 AND workflow_coverage IS NOT NULL
                    AND NOT consumed ORDER BY id LIMIT 51''', auth.lane, auth.owner, cursor)
                if not rows:
                    break
                for row in rows:
                    cursor = row['id']
                    try:
                        record = await self.proposal_record(c, auth, cursor)
                    except ExecutionError as exc:
                        if exc.status == 404:
                            continue
                        raise
                    proposal = record['proposal']
                    if proposal['state'] == 'proposed':
                        items.append({'kind': 'workflow_proposal', **proposal})
                    if len(items) > limit:
                        break
                if len(rows) < 51 or len(items) > limit:
                    break
        return wire_integers({'items': items[:limit],
            'next_cursor': items[limit-1]['id'] if len(items) > limit else None})

    async def proposal_history(self, auth, cursor, limit):
        # History cursors contain a stable timestamp/id pair. They do not depend
        # on an offset or on the continued visibility of the previous page.
        before = None; before_id = ''
        if cursor:
            try:
                value = json.loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
                before, before_id = utc(value[0]), identifier(value[1])
                if len(value) != 2:
                    raise ValueError()
            except (ValueError, TypeError, IndexError, KeyError, ExecutionError):
                raise ExecutionError('invalid_cursor') from None
        items = []; now = self.clock()
        async with self.store.transaction() as c:
            while len(items) <= limit:
                rows = await c.fetch('''SELECT id,
                    (workflow_coverage->'proposal'->>'expires_at')::timestamptz AS expires_at
                    FROM agent_admission_intents
                    WHERE lane=$1 AND owner=$2 AND workflow_coverage IS NOT NULL AND NOT consumed
                    AND workflow_coverage->'proposal'->>'state' IN ('proposed','expired')
                    AND (workflow_coverage->'proposal'->>'run_id') IS NULL
                    AND (workflow_coverage->'proposal'->>'expires_at')::timestamptz >= $3
                    AND ($4::timestamptz IS NULL OR
                        ((workflow_coverage->'proposal'->>'expires_at')::timestamptz,id)<($4,$5))
                    ORDER BY expires_at DESC,id DESC LIMIT 51''',
                    auth.lane, auth.owner, now-timedelta(days=7), before, before_id)
                if not rows:
                    break
                for row in rows:
                    before, before_id = row['expires_at'], row['id']
                    try:
                        record = await self.proposal_record(c,auth,before_id)
                    except ExecutionError as exc:
                        if exc.status == 404:
                            continue
                        raise
                    p = record['proposal']
                    if (p['state'] == 'proposed' or (p['state'] == 'expired'
                            and now-timedelta(days=7) <= utc(p['expires_at']) <= now)) and not p.get('run_id'):
                        items.append({'kind':'workflow_proposal',**p})
                    if len(items) > limit:
                        break
                if len(rows) < 51 or len(items) > limit:
                    break
        last = items[limit-1] if len(items) > limit else None
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [last['expires_at'],last['id']]).encode()).decode() if last else None
        return wire_integers({'items':items[:limit],'next_cursor':next_cursor})
