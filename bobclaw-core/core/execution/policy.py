"""Versioned policy ceiling; never supplies missing credential or grant authority."""
import json
import os
from pathlib import Path
import uuid

from core.permissions import Scope, evaluate_action, evaluate_path
from .models import ExecutionError, integer, only, request_key

POOL = None
ACTIONS = frozenset({'read', 'write', 'cc_edit', 'model_call', 'shell', 'python', 'git',
                     'codex', 'mcp', 'email_send', 'email_reply', 'form_submit', 'purchase',
                     'file_delete', 'shell_dangerous', 'merge_to_main'})
RESTRICTIONS = {'scope_intersection': True, 'owner_controls_grantable': False,
                'token_minting_grantable': False, 'provider_enrollment_grantable': False,
                'cross_lane_grantable': False, 'worker_escape_grantable': False,
                'always_human': ['email_send', 'email_reply', 'form_submit', 'purchase',
                                 'file_delete', 'shell_dangerous', 'merge_to_main']}


def projection(value=None, revision=0):
    return {'policy': value, 'revision': revision, 'immutable_restrictions': RESTRICTIONS}


def ceiling(value, action, paths=()):
    if value is None or value.get('posture') == 'inherit':
        return 'auto'  # No editable policy installed: preserve existing authority checks.
    if value['posture'] == 'confirm':
        return 'human'
    scope = Scope.model_validate(value['scope'])
    destinations = [evaluate_action(action, scope), *(evaluate_path(str(Path(p).resolve()), scope) for p in paths)]
    return max(destinations, key={'auto': 0, 'gate': 1, 'human': 2}.__getitem__)


async def normalize(conn, auth, value):
    only(value, {'posture', 'scope'})
    if value.get('posture') not in {'inherit', 'scoped', 'confirm'}:
        raise ExecutionError('invalid_policy')
    raw = value.get('scope')
    only(raw, {'auto_actions', 'escalate_actions', 'may_touch', 'may_not_touch'})
    if value['posture'] == 'inherit' and any(raw.values()):
        raise ExecutionError('invalid_policy')
    result = {}
    for field in ('auto_actions', 'escalate_actions'):
        actions = raw.get(field, [])
        if (not isinstance(actions, list) or len(actions) > len(ACTIONS)
                or any(not isinstance(a, str) or a not in ACTIONS for a in actions)):
            raise ExecutionError('invalid_policy_action')
        result[field] = sorted(set(actions))
    from core.portal_resources import load_folders, relative_parts, safe_open
    lane_root = Path(os.environ.get('BOB_LANE_ROOT', '.'))
    folders = {row['attachment_id']: row for row in load_folders(lane_root)}
    for field in ('may_touch', 'may_not_touch'):
        rules = raw.get(field, [])
        if not isinstance(rules, list) or len(rules) > 100:
            raise ExecutionError('invalid_policy_path')
        resolved = []
        for rule in rules:
            only(rule, {'attachment_id', 'path'})
            folder = folders.get(rule.get('attachment_id'))
            if not folder or folder['owner'] != auth.owner:
                raise ExecutionError('resource_not_found', 404)
            owned = await conn.fetchval('SELECT 1 FROM projects WHERE id=$1 AND user_id=$2 AND is_archived=FALSE',
                                        uuid.UUID(folder['project_id']), auth.owner)
            if not owned:
                raise ExecutionError('resource_not_found', 404)
            # Patterns are limited to an optional final /** under an installed root.
            path = rule.get('path')
            if not isinstance(path, str):
                raise ExecutionError('invalid_policy_path')
            recursive = path == '**' or path.endswith('/**')
            plain = '' if path == '**' else path[:-3] if recursive else path
            parts = relative_parts(plain, empty=True)
            if any('*' in p or '?' in p or '[' in p for p in parts):
                raise ExecutionError('invalid_policy_path')
            registered = relative_parts(folder['path'])
            with safe_open(lane_root, registered, directory=True):
                pass
            root = lane_root.joinpath(*registered).resolve()
            target = root.joinpath(*parts).resolve()
            if not target.is_relative_to(root):
                raise ExecutionError('invalid_policy_path')
            resolved.append(str(target) + ('/**' if recursive else ''))
        result[field] = sorted(set(resolved))
    return {'posture': value['posture'], 'scope': result}


async def current(conn, lane, owner):
    row = await conn.fetchrow('SELECT v.* FROM owner_policy_heads h JOIN owner_policy_versions v '
                              'USING(lane,owner,revision) WHERE h.lane=$1 AND h.owner=$2', lane, owner)
    if not row:
        return projection()
    value = json.loads(row['policy']) if isinstance(row['policy'], str) else row['policy']
    return projection(value, row['revision'])


async def gate_ceiling(action, paths):
    if not os.environ.get('BOB_SETTINGS_ID'):
        return 'auto'
    if POOL is None:
        return 'human'
    from .api import lane_id
    async with POOL.acquire() as conn:
        value = await current(conn, lane_id(), os.environ.get('BOB_DEFAULT_OWNER', ''))
    return ceiling(value['policy'], action, paths)


class Policies:
    async def get_policy(self, auth, args):
        only(args, set())
        async with self.transaction() as conn:
            return await current(conn, auth.lane, auth.owner)

    async def set_policy(self, auth, args):
        only(args, {'policy', 'expected_revision', 'request_key'})
        key = request_key(args.get('request_key'))
        expected = integer(args.get('expected_revision'), 0)
        # Shares grant/dispatch's exclusive side BEFORE run/pool locks.
        async with self.transaction(exclusive_grants=True) as conn:
            previous = await self._mutation(conn, auth, 'policy.set', key, args)
            if previous:
                return previous['result']
            head = await current(conn, auth.lane, auth.owner)
            if expected != head['revision']:
                raise ExecutionError('revision_conflict', 409)
            value = await normalize(conn, auth, args.get('policy'))
            revision = expected + 1
            await conn.execute('INSERT INTO owner_policy_versions(lane,owner,revision,policy) VALUES ($1,$2,$3,$4::jsonb)',
                               auth.lane, auth.owner, revision, json.dumps(value))
            await conn.execute('INSERT INTO owner_policy_heads VALUES ($1,$2,$3) ON CONFLICT(lane,owner) '
                               'DO UPDATE SET revision=excluded.revision', auth.lane, auth.owner, revision)
            # A narrowed run stays paused after widening; only explicit Resume can
            # reauthorize it. In-flight provider effects keep their accounting.
            rows = await conn.fetch("SELECT * FROM agent_runs WHERE lane=$1 AND owner=$2 AND status='running' FOR UPDATE",
                                    auth.lane, auth.owner)
            affected = []
            for raw in rows:
                from .store import record
                row = record(raw)
                keys = (auth.lane, auth.owner, row['id'])
                worker_pending = await conn.fetchval("SELECT count(*) FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND outcome IN ('dispatch_intent','running')", *keys)
                if worker_pending and value != head['policy']:
                    await conn.execute('UPDATE agent_runs SET cleanup_pending=TRUE WHERE lane=$1 AND owner=$2 AND id=$3', *keys)
                if ceiling(value, 'model_call') != 'auto':
                    await self._lock_pools(conn, keys)
                    pending = await conn.fetchval("SELECT count(*) FROM agent_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'", *keys)
                    if pending:
                        await conn.execute("UPDATE agent_attempts SET state='unknown' WHERE lane=$1 AND owner=$2 AND run_id=$3 AND state='dispatched'", *keys)
                        await conn.execute('UPDATE agent_budget_accounts SET unknown_calls=unknown_calls+$4 WHERE lane=$1 AND owner=$2 AND run_id=$3', *keys, pending)
                    cleanup_pending = bool(pending or worker_pending)
                    await conn.execute('UPDATE agent_runs SET cleanup_pending=$4 WHERE lane=$1 AND owner=$2 AND id=$3', *keys, cleanup_pending)
                    await self._pause(conn, row, 'policy_narrowed', owner_flag=True,
                                      detail={'policy_revision': revision, 'cleanup_pending': cleanup_pending})
                    affected.append(row['id'])
            # Existing active worker jobs may have filesystem effects. Request
            # cancellation, retain all uncertainty, and let their controller reap.
            if value != head['policy']:
                await conn.execute("UPDATE worker_job_attempts SET cancel_requested=TRUE WHERE lane=$1 AND outcome IN ('queued','dispatch_intent','running')",
                                   auth.lane)
            return await self._remember(conn, auth, 'policy.set', key, args, auth.owner,
                                        {**projection(value, revision), 'paused_runs': affected})

    async def check_policy(self, conn, row):
        if not os.environ.get('BOB_SETTINGS_ID'):
            return
        value = await current(conn, row['lane'], row['owner'])
        if ceiling(value['policy'], 'model_call') != 'auto':
            raise ExecutionError('policy_narrowed', 409)
