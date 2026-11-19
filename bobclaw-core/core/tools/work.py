"""Small typed work surface; no filesystem paths, shell or owner-grant minting."""
import json
from typing import Literal, Optional
from langchain_core.tools import tool

from core.execution.chat_context import call


@tool
async def work_catalog() -> str:
    """Read current recipes, owned projects/workspaces, teams and execution readiness."""
    return json.dumps(await call('work.catalog',{}))


@tool
async def work_recipe_save(name: str, checks: list[Literal['python.compile','python.unittest']],
                           expected_revision: Optional[int], request_key: str) -> str:
    """Save a named pipe from installed Python checks; never starts work or adds executables.

    Use the returned exact revision. expected_revision is null for a new recipe.
    """
    return json.dumps(await call('work.recipe.save',dict(name=name,checks=checks,
        expected_revision=expected_revision,request_key=request_key)))


@tool
async def work_prepare(project_id: str, task: str, mode: Literal['builder','fixer'],
                       recipe: str, request_key: str, start_calls: int = 10) -> str:
    """Prepare a bounded Python build/fix and its reviewable owner setup/approval card.

    Uses a BoB lane workspace, not an open Windows folder. Missing workspace or
    grant returns the exact next owner action. Preparation never starts a job.
    """
    return json.dumps(await call('work.prepare',dict(project_id=project_id,task=task,
        mode=mode,recipe=recipe,request_key=request_key,start_calls=start_calls)))


@tool
async def work_start(preparation_id: str, request_key: str) -> str:
    """Start the exact prepared work after matching owner authorization; save returned job/run IDs.

    Accepted is not completed. Read status until a terminal receipt, and never
    claim files were published until a publication receipt exists.
    """
    return json.dumps(await call('work.start',dict(preparation_id=preparation_id,request_key=request_key)))


@tool
async def work_status(job_id: str) -> str:
    """Read owned job state, diff/check receipts and any managed-workspace publication."""
    return json.dumps(await call('work.status',{'job_id':job_id}))


@tool
async def work_cancel(job_id: str, request_key: str) -> str:
    """Cancel an owned work job. Report returned terminal/cleanup state, not merely acknowledgement."""
    return json.dumps(await call('jobs.cancel',dict(job_id=job_id,request_key=request_key)))


@tool
async def work_team_save(name: str, manager: str, worker: str, critic: str,
                         expected_revision: Optional[int], request_key: str) -> str:
    """Save a supplied-evidence text team using exact catalog binding IDs. Same model is allowed.

    This saves a team only. For coding use a named Python recipe and work_prepare.
    """
    return json.dumps(await call('teams.save',{'definition':{'schema_version':1,'id':name,
        'state':'saved','topology':'four-step-readonly/v1','roles':dict(manager=manager,worker=worker,critic=critic),
        'review_independence':'same_model'},'expected_revision':expected_revision,'request_key':request_key}))


@tool
async def work_team_start(team: str, team_revision: int, task: str, request_key: str,
                          start_calls: int = 10) -> str:
    """Start a bounded saved text team, preserving supplied evidence/source IDs in task. No filesystem actions.

    For a Python build/fix pipe use work_prepare/work_start instead. Accepted is
    only admission; save the run ID and inspect its terminal receipt.
    """
    return json.dumps(await call('runs.start',{'contract_version':2,'team':team,'team_revision':team_revision,
        'task':task,'request_key':request_key,'budget':{'start_calls':start_calls,'wall_mode':'fixed','wall_seconds':900}}))


@tool
async def work_run_status(run_id: str) -> str:
    """Read an owned team run and its real receipts; completed is not independently verified."""
    return json.dumps(await call('runs.get',{'run_id':run_id}))


@tool
async def work_run_cancel(run_id: str, request_key: str) -> str:
    """Cancel an owned team run and inspect returned state."""
    return json.dumps(await call('runs.cancel',dict(run_id=run_id,request_key=request_key)))
