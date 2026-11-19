"""Trusted gateway lifecycle hooks may only reduce existing session authority."""
import uuid


async def end_sessions(pool, lane, owner, conversations=None, *, reason='stopped'):
    if pool is None or conversations == []:
        return
    ids=[uuid.UUID(str(value)) for value in conversations] if conversations is not None else None
    from core.execution.models import canonical
    async with pool.acquire() as c, c.transaction():
        await c.execute('''UPDATE pc_tasks SET ended_at=extract(epoch FROM now())
            WHERE lane=$1 AND owner=$2 AND ended_at IS NULL
            AND ($3::uuid[] IS NULL OR conversation_id=ANY($3))''',lane,owner,ids)
        await c.execute('''UPDATE pc_requests r SET state=CASE WHEN r.state='running' THEN 'unknown' ELSE 'stopped' END
            FROM pc_tasks t WHERE r.task_id=t.id AND t.lane=$1 AND t.owner=$2 AND t.ended_at IS NOT NULL
            AND r.state IN('pending','running')''',lane,owner)
        # Do not wait for a long-running worker's advisory lock. Revoke future
        # dispatch immediately; an in-flight mutation still needs its receipt.
        await c.execute('''UPDATE builder_modes SET session_until=0,
            mode=CASE WHEN mode='yolo' THEN 'auto' ELSE mode END,
            expires_at=NULL,revision=revision+1 WHERE lane=$1 AND owner=$2
            AND ($3::uuid[] IS NULL OR conversation_id=ANY($3))''',lane,owner,ids)
        await c.execute('''UPDATE builder_approvals SET state='stopped',execution_state='stopped',
            result=$4::jsonb WHERE lane=$1 AND owner=$2
            AND ($3::uuid[] IS NULL OR conversation_id=ANY($3))
            AND state IN ('pending','allowed') AND execution_state='pending' ''',
            lane,owner,ids,canonical({'status':'stopped','end_turn':True,'error':{'code':reason}}))
