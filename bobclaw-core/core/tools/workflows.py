"""Model workflow tools. Owner clicks are deliberately absent."""
from typing import Literal
from langchain_core.tools import tool, StructuredTool
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr
from core.tools.workflow_context import call


class Args(BaseModel):
    model_config = ConfigDict(extra='forbid')


class StrictEmptyTool(StructuredTool):
    def _to_args_and_kwargs(self, tool_input, tool_call_id):
        # LangChain skips validation for an empty BaseModel schema by default.
        self.args_schema.model_validate(tool_input)
        return super()._to_args_and_kwargs(tool_input, tool_call_id)


class ListArgs(Args):
    cursor: StrictStr | None = None
    limit: StrictInt = 20


class GetArgs(Args):
    id: StrictStr
    revision: StrictInt | None = None


class ProposeArgs(Args):
    inputs: dict
    bindings: dict[str, str]
    expires_at: StrictStr
    request_key: StrictStr
    definition_ref: dict | None = None
    definition: dict | None = None
    project_id: StrictStr | None = None
    workspace: dict | None = None
    run_mode: Literal['auto', 'approve'] = 'auto'


class RunArgs(Args):
    run_id: StrictStr


class StopArgs(RunArgs):
    request_key: StrictStr


class MessageArgs(StopArgs):
    stage_key: StrictStr
    text: StrictStr


@tool(args_schema=Args)
async def workflows_catalog() -> dict:
    """Read workflow premades, enrolled bindings, availability and limits before suggesting a run.

    The platform checks the review policy when a proposal is made. An unmet
    policy needs the owner's explicit same-model review choice, which is recorded.
    """
    return await call('catalog', {})


workflows_catalog = StrictEmptyTool(name=workflows_catalog.name,
    description=workflows_catalog.description, args_schema=Args,
    coroutine=workflows_catalog.coroutine)


@tool(args_schema=ListArgs)
async def workflows_list(cursor: str | None = None, limit: int = 20) -> dict:
    """List saved workflow definitions visible to this caller, with pagination."""
    return await call('list', {**({'cursor': cursor} if cursor else {}), 'limit': limit})


@tool(args_schema=GetArgs)
async def workflows_get(id: str, revision: int | None = None) -> dict:
    """Read a visible workflow definition and its exact version before proposing it."""
    return await call('get', {'id': id, **({'revision': revision} if revision is not None else {})})


@tool(args_schema=ProposeArgs)
async def workflows_propose(inputs: dict, bindings: dict[str, str], expires_at: str, request_key: str,
        definition_ref: dict | None = None, definition: dict | None = None, project_id: str | None = None,
        workspace: dict | None = None, run_mode: str = 'auto') -> dict:
    """Propose a workflow for multi-stage build, review and fix or longer unattended work.

    Plan with the user first. Explain what it will do, its call/time cost and what
    remains unverified. Choose exactly one definition or definition_ref. This
    returns frozen coverage and an owner review card; only the user's Start click
    runs it. Show the returned action in a bob-action fence. The authenticated
    conversation supplies the origin; it cannot be chosen by a tool argument.

    Minimal new-workspace example (substitute binding IDs from workflows_catalog and
    replace expires_at with server UTC time plus one hour, within catalog limits):
    {"definition_ref":{"id":"premade-plan-build-review-fix","revision":1},
     "inputs":{"task":"Create hello.txt containing hello"},
     "bindings":{"planner":"planner-binding-id","builder":"builder-binding-id","reviewer":"reviewer-binding-id"},
     "workspace":{"create":"Hello workflow"},
     "expires_at":"2026-11-18T12:00:00Z","request_key":"hello-workflow-001"}
    workspace has exactly the key create; omit project_id for a new workspace.
    definition_ref needs both id and revision; omit definition when using it.
    expires_at must be future UTC ending in Z or +00:00, not a local offset.
    Use fresh request_key and arguments for a corrected refusal; replay an identical
    request_key and identical arguments only after an uncertain acknowledgement.
    """
    args = dict(inputs=inputs, bindings=bindings, expires_at=expires_at,
        request_key=request_key, run_mode=run_mode)
    args.update({k: v for k, v in dict(definition_ref=definition_ref, definition=definition,
        project_id=project_id, workspace=workspace).items() if v is not None})
    return await call('propose', args)


@tool(args_schema=RunArgs)
async def workflow_run_status(run_id: str) -> dict:
    """Read an authorized run, its budget, stages and Needs you items. Accepted is not completed."""
    return await call('run', {'run_id': run_id})


@tool(args_schema=RunArgs)
async def workflow_run_report(run_id: str) -> dict:
    """Read the authorized run's report, or null before it exists. Report checks and unverified work honestly."""
    return await call('report', {'run_id': run_id})


@tool(args_schema=MessageArgs)
async def workflow_run_message(run_id: str, stage_key: str, text: str, request_key: str) -> dict:
    """Queue steering only for a run originating in this Assistant or Planner conversation.

    A message cannot answer owner gates, widen authority or replace acceptance.
    Reuse the same request key and arguments after a lost acknowledgement.
    """
    return await call('message', dict(run_id=run_id, stage_key=stage_key, text=text, request_key=request_key))


@tool(args_schema=StopArgs)
async def workflow_run_stop(run_id: str, request_key: str) -> dict:
    """Request stop only for a run originating in this Assistant or Planner conversation. Inspect the returned cleanup and terminal state."""
    return await call('stop', dict(run_id=run_id, request_key=request_key))


WORKFLOW_TOOLS = [workflows_catalog, workflows_list, workflows_get, workflows_propose,
    workflow_run_status, workflow_run_report, workflow_run_message, workflow_run_stop]
WORKFLOW_READ_NAMES = ['workflows_catalog', 'workflows_list', 'workflows_get',
    'workflow_run_status', 'workflow_run_report']
