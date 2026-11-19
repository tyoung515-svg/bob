"""Process-local workflow capability. It is never accepted from an RPC or model."""
from contextvars import ContextVar

from .models import ExecutionError, digest

current = ContextVar('workflow_stage_context', default=None)


class WorkflowParked(Exception):
    """The stage is durably waiting and has no remaining in-process work."""


async def writer_guard(connection, auth, project, *, run_id=None, generation=None):
    """Caller holds the existing Builder project mutation advisory lock.

    Expired leases are deliberately not ignored: a dead writer needs recovery
    and cleanup before another writer may enter its workspace.
    """
    lease = await connection.fetchrow('''SELECT l.* FROM workflow_workspace_leases l
        JOIN agent_runs r ON (r.lane,r.owner,r.id)=(l.lane,l.owner,l.run_id)
        WHERE l.lane=$1 AND l.owner=$2 AND r.request->>'project_id'=$3''',
        auth.lane, auth.owner, str(project))
    if lease and (lease['run_id'] != run_id or lease['quarantined']):
        raise ExecutionError('workspace_busy', 409,
                             'This workflow owns writes in this workspace. Wait for it to finish or stop it.')


def mutation_lock(auth, project):
    return int(digest([auth.lane, str(project), 'builder-mutation'])[:15], 16)


async def conversation_run(pool,owner,conversation):
    if pool is None or not owner: return None
    import uuid
    try: identifier=uuid.UUID(conversation)
    except (ValueError,TypeError,AttributeError): return None
    row=await pool.fetchrow('''SELECT r.id AS run_id,r.topology FROM agent_run_steps s JOIN agent_runs r
        ON (r.lane,r.owner,r.id)=(s.lane,s.owner,s.run_id) WHERE s.conversation_id=$1 AND r.owner=$2
        AND r.topology='workflow/v1' AND r.status NOT IN ('completed','stopped','expired','failed','blocked','unknown') LIMIT 1''',identifier,owner)
    return row['run_id'] if row and row.get('topology')=='workflow/v1' else None
