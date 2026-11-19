"""Scoped workflow MCP proxies. Gateway authentication is the authority boundary."""
import json
from typing import Literal
from urllib.parse import quote


def register(mcp, gateway_json):
    async def read(path, **params):
        return await gateway_json('GET', path, params={'contract_version': 1,
            **{k: v for k, v in params.items() if v is not None}})

    async def write(path, **body):
        return await gateway_json('POST', path, json_body={'contract_version': 1,
            **{k: v for k, v in body.items() if v is not None}})

    def run_path(run_id):
        from core.execution.workflows import identifier
        return '/workflow-runs/' + quote(identifier(run_id), safe='')

    @mcp.tool()
    async def workflows_catalog() -> str:
        """Read workflow premades, enrolled bindings, availability and limits within this token's scope.

        The platform checks the review policy when a proposal is made. An unmet
        policy needs the owner's explicit same-model review choice, which is recorded.
        """
        return await read('/workflows/catalog')

    @mcp.tool()
    async def workflows_list(cursor: str | None = None, limit: int = 20) -> str:
        """List saved workflow definitions visible to this token; follow next_cursor."""
        return await read('/workflows', cursor=cursor, limit=limit)

    @mcp.tool()
    async def workflows_get(id: str, revision: int | None = None) -> str:
        """Read a visible workflow definition and its frozen revision."""
        from core.execution.workflows import identifier
        return await read('/workflows/' + quote(identifier(id), safe=''), revision=revision)

    @mcp.tool()
    async def workflows_propose(inputs: dict, bindings: dict[str, str], expires_at: str, request_key: str,
            definition_ref: dict | None = None, definition: dict | None = None,
            project_id: str | None = None, workspace: dict | None = None,
            run_mode: Literal['auto', 'approve'] = 'auto') -> str:
        """Propose multi-stage build, review and fix or longer unattended work after planning with the user.

        Explain what will run, its call/time budget and what stays unverified.
        Supply exactly one definition choice. Returns frozen proposal coverage;
        it appears in the owner's Needs you with this token's identity. Only the
        owner's Start click runs it. MCP cannot choose an Assistant origin or
        start, resume, answer gates, save, copy or delete definitions.

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
        return await write('/workflow-proposals', inputs=inputs, bindings=bindings,
            expires_at=expires_at, request_key=request_key, definition_ref=definition_ref,
            definition=definition, project_id=project_id, workspace=workspace, run_mode=run_mode)

    @mcp.tool()
    async def workflow_run_status(run_id: str) -> str:
        """Read a scoped run's stages, budget and Needs you items. Accepted is not completed."""
        return await read(run_path(run_id))

    @mcp.tool()
    async def workflow_run_events(run_id: str, after_seq: int = 0, limit: int = 100, wait: float = 0) -> str:
        """Read scoped durable events after a sequence, optionally waiting up to 25 seconds.

        Keep next_seq on reconnect, respect rate-limit retry delays, and never
        cancel a run because observation ends.
        """
        return await read(run_path(run_id) + '/events', after_seq=after_seq, limit=limit, wait=wait)

    @mcp.tool()
    async def workflow_run_report(run_id: str) -> str:
        """Read the scoped run's report or null. Completion does not imply independent verification."""
        raw = await read(run_path(run_id))
        if raw.startswith('[error:'):
            return raw
        try:
            return json.dumps({'report': json.loads(raw)['run']['report']})
        except (ValueError, KeyError, TypeError):
            return '[error:bad_upstream] Missing workflow report field'

    @mcp.tool()
    async def workflow_run_message(run_id: str, stage_key: str, text: str, request_key: str) -> str:
        """Queue steering within this token's initiating scope, without answering gates or widening the grant.

        A terminal target refuses messages. Replay the exact same request key and
        arguments after a lost acknowledgement.
        """
        return await write(run_path(run_id) + '/messages', stage_key=stage_key,
            text=text, request_key=request_key)

    @mcp.tool()
    async def workflow_run_stop(run_id: str, request_key: str) -> str:
        """Request stop within this token's initiating scope. Inspect terminal and cleanup state; acknowledgement is not proof."""
        return await write(run_path(run_id) + '/stop', request_key=request_key)

    # Reject extra fields at the MCP argument boundary, including forged origin,
    # identity and owner-session fields. FastMCP otherwise ignores extra keys.
    names = {f.__name__ for f in (workflows_catalog, workflows_list, workflows_get,
        workflows_propose, workflow_run_status, workflow_run_events,
        workflow_run_report, workflow_run_message, workflow_run_stop)}
    for tool in mcp._tool_manager.list_tools():
        if tool.name in names:
            tool.fn_metadata.arg_model.model_config['extra'] = 'forbid'
            tool.fn_metadata.arg_model.model_rebuild(force=True)
