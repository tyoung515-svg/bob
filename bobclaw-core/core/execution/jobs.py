"""Internal worker operation identity and service-selected job policy; no Jobs API."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import uuid

from core.worker.contract import WorkerUnavailable


@dataclass(frozen=True)
class JobContext:
    owner: str
    principal: str
    group: str
    boundary: str
    run_id: str | None = None
    run_generation: int | None = None
    policy: object | None = None


current_job = ContextVar('worker_operation_context', default=None)


@contextmanager
def job_context(context):
    token = current_job.set(context)
    try:
        yield
    finally:
        current_job.reset(token)


def operation_context():
    context = current_job.get()
    if context:
        return context
    from .call_guard import required
    if required.get():
        raise WorkerUnavailable('worker run context required')
    from core.tools.projects import _current_user_id
    principal = _current_user_id.get()
    # Legacy internal callers have no vouched user. Conservatively share a single
    # service principal/group; conversation IDs cannot manufacture queue priority.
    principal = str(principal or 'lane-service')
    return JobContext(principal, principal, 'chat:' + principal, 'internal-worker-client')


def new_execution_id():
    return uuid.uuid4().hex
