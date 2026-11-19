"""BoBClaw Core — MCP server (provider side, Neck Beard MODE).

Publishes Bob's chat surface as MCP tools over **stdio** so any MCP-capable
agent (Claude Code, Codex, "ownclaw", …) can drive Bob headlessly. This is the
inverse of ``core/mcp/client.py`` (the consumer): here Bob is the *provider*.

Each tool is a thin proxy to the gateway's ``/ws/chat`` WebSocket — the single
authoritative LangGraph/checkpointer — authenticated by a **scoped agent bearer
token** (see ``bobclaw-gateway`` ``auth.create_agent_token``). We deliberately do
NOT invoke the graph in-process: proxying through the gateway keeps one graph,
one auth boundary, and one streaming contract.

v0 publishes exactly two tools: ``chat_with_face`` and ``run_council``. The
token's ``faces`` allow-list (decoded WITHOUT verifying the signature — the
gateway verifies on use; here we only filter the surface) gates which tools are
registered and which ``face_id`` values ``chat_with_face`` will proxy. The hard
security boundary is the gateway (Phase-1 default-deny + Phase-3 scope), not this
client-side filter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import aiohttp
import jwt
from mcp.server.fastmcp import FastMCP

from core.paths import family_memory_path

logger = logging.getLogger("bobclaw.mcp")

# IPv4 literal — the gateway binds 0.0.0.0 IPv4-only; "localhost" can resolve to
# ::1 and fail to connect. Mirror the rest of Bob's client code.
DEFAULT_GATEWAY = "http://127.0.0.1:7826"
COUNCIL_FACE = "council-max"
# Default wall-clock ceiling for one proxied turn (seconds). A comfortable
# multiple of the gateway's own 600s sock_read so legitimate slow council/
# grounding turns aren't cut short. Overridable via BOBCLAW_MCP_TURN_TIMEOUT.
DEFAULT_TURN_TIMEOUT = 1800
# No total cap + a generous per-read at the socket layer: a CoCouncil restart
# turn can go silent for minutes during grounding spawns. The real ceiling is the
# wall-clock asyncio.wait_for in _chat_turn — sock_read alone is defeated by the
# gateway's 30s heartbeat PINGs (each resets the read timer).
_WS_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=600)
# Short ceiling for the read-only REST proxies (faces/models/system): these
# are small JSON round-trips, never streams.
_REST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class _UpstreamError(RuntimeError):
    """A non-2xx HTTP response from the gateway, carrying the status code so the
    proxy can surface the Phase-1 boundary (403/401) as a legible in-band error."""

    def __init__(self, status: int, body: str):
        self.status = status
        super().__init__(f"{status} {body}".strip())


class MCPConfig:
    """Resolved server configuration: the agent token, the gateway base URL, the
    token's faces allow-list (empty ⇒ unrestricted), and the per-turn ceiling."""

    def __init__(
        self,
        token: str,
        gateway: str,
        faces: list[str],
        turn_timeout: int = DEFAULT_TURN_TIMEOUT,
        inner_open: bool = False,
        scope: Optional[dict] = None,
        work_read_tools: bool = True,
    ):
        self.token = token
        self.gateway = gateway.rstrip("/")
        self.faces = faces
        self.turn_timeout = turn_timeout
        self.scope = scope or {}
        self.work_read_tools = work_read_tools
        # PATCH-LATER: household-trust "open" mode
        # skips the local face allow-list; the gateway is relaxed identically.
        # Re-enable strict after the scope-authority migration.
        self.inner_open = inner_open
        # Per-face default conversation, lazily created and reused so repeated
        # tool calls form a continuous thread (one stdio server = one agent).
        self._default_convs: dict[str, str] = {}

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def personal_memory_allowed(self) -> bool:
        """True when the token's scope reads personal family memory.

        Gates the READ-ONLY work-lane tools below. Surface filter only — the
        gateway re-checks scope on every call, so a work token that somehow got
        these tools still gets 403 from the server.
        """
        import fnmatch

        selected = family_memory_path()
        if selected is None:
            return False
        path = str(selected)
        for pattern in self.scope.get("may_not_touch") or []:
            if isinstance(pattern, str) and fnmatch.fnmatch(path, pattern):
                return False
        return any(
            isinstance(pattern, str) and fnmatch.fnmatch(path, pattern)
            for pattern in self.scope.get("may_touch") or []
        )

    @property
    def work_read_allowed(self) -> bool:
        """Whether the READ-ONLY work-lane tools are exposed on this surface.

        Two conditions: the token reads personal memory (personal may look into
        work), AND the operator has not killed the crossing with
        ``BOB_WORK_READ_TOOLS=0``. The switch exists because a personal CLOUD
        model that reads work-lane text ships SPS content to its provider; the
        barrier is per-surface, not per-request.
        """
        return self.personal_memory_allowed and self.work_read_tools

    def face_allowed(self, face_id: str) -> bool:
        # Empty allow-list ⇒ unrestricted (the admin who minted the token did not
        # constrain faces). A non-empty list is a strict allow-list.
        # inner_open (PATCH-LATER 2026-10-16) ⇒ unrestricted.
        if self.inner_open:
            return True
        return not self.faces or face_id in self.faces

    @property
    def council_allowed(self) -> bool:
        # Consistent with the face actually invoked: run_council proxies
        # COUNCIL_FACE, so it is allowed iff that exact face is in the allow-list
        # (or the token is unrestricted). A token scoped to e.g. council-lite does
        # NOT get run_council, which runs council-max.
        return self.face_allowed(COUNCIL_FACE)


def _decode_faces(token: str) -> list[str]:
    """Read the ``faces`` claim WITHOUT verifying the signature.

    The MCP server holds the token but not the gateway's JWT secret, so it cannot
    (and need not) verify — the gateway verifies on every call. We only need
    ``faces`` to decide which tools to expose. Returns ``[]`` (unrestricted) if
    the token can't be decoded; the gateway rejects a bad token on use anyway.
    """
    try:
        payload = jwt.decode(
            token, options={"verify_signature": False, "verify_exp": False}
        )
        faces = payload.get("faces") or []
        return [f for f in faces if isinstance(f, str)]
    except Exception as exc:  # noqa: BLE001 — malformed ⇒ gateway rejects on use
        logger.debug("could not decode faces claim: %s", exc)
        return []


def _decode_scope(token: str) -> dict:
    """Read the ``scope`` claim WITHOUT verifying the signature (surface filter only).

    Same posture as ``_decode_faces``: the gateway verifies on every call and is the
    real boundary. Returns ``{}`` when unreadable, so a work tool is never exposed by
    accident.
    """
    try:
        payload = jwt.decode(
            token, options={"verify_signature": False, "verify_exp": False}
        )
        scope = payload.get("scope")
        return scope if isinstance(scope, dict) else {}
    except Exception as exc:  # noqa: BLE001 — malformed ⇒ gateway rejects on use
        logger.debug("could not decode scope claim: %s", exc)
        return {}


def load_config(env: Optional[dict] = None) -> MCPConfig:
    """Build the config from the environment. Raises SystemExit if no token."""
    env = env or os.environ
    token = (env.get("BOBCLAW_AGENT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "BOBCLAW_AGENT_TOKEN is required -- mint one via POST /auth/agent-token "
            "(admin-authed) and export it before launching the MCP server."
        )
    gateway = (env.get("BOBCLAW_GATEWAY") or DEFAULT_GATEWAY).strip()
    try:
        turn_timeout = int(env.get("BOBCLAW_MCP_TURN_TIMEOUT") or DEFAULT_TURN_TIMEOUT)
    except ValueError:
        turn_timeout = DEFAULT_TURN_TIMEOUT
    return MCPConfig(
        token=token,
        gateway=gateway,
        faces=_decode_faces(token),
        turn_timeout=turn_timeout,
        inner_open=(env.get("BOB_AGENT_INNER_GATES") or "").strip().lower() == "open",
        scope=_decode_scope(token),
        work_read_tools=(env.get("BOB_WORK_READ_TOOLS") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
    )


# ── WS proxy helpers (factored small for unit testing) ───────────────────────

async def _create_conversation(session, cfg: MCPConfig, title: str) -> str:
    async with session.post(
        f"{cfg.gateway}/conversations", json={"title": title}, headers=cfg.headers
    ) as r:
        if r.status >= 400:
            raise _UpstreamError(r.status, await r.text())
        return (await r.json())["id"]


async def _consume_stream(ws) -> str:
    """Accumulate ``chunk`` frames until a terminal frame; surface a core
    ``error`` frame as an ``[error:code] message`` string. If the socket closes
    WITHOUT a terminal frame, mark the partial so a truncated reply is never
    mistaken for a complete one."""
    parts: list[str] = []
    completed = False
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            continue
        try:
            evt = json.loads(msg.data)
        except (ValueError, TypeError):
            continue  # skip a malformed frame rather than abort the whole turn
        etype = evt.get("type")
        if etype == "chunk":
            parts.append(evt.get("content", ""))
        elif etype == "message_complete":
            completed = True
            break
        elif etype == "error":
            return f"[error:{evt.get('code')}] {evt.get('message')}"
        elif etype == "generation_stopped":
            completed = True  # a deliberate stop is a terminal frame
            break
    text = "".join(parts)
    if not completed:
        marker = "[error:connection_closed] stream ended before completion"
        return f"{text}\n{marker}" if text else marker
    return text


async def _do_chat_turn(cfg: MCPConfig, session, message: str, face_id: str, conversation_id: str) -> str:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        # Reuse this session's default thread for the face so repeated calls
        # continue the same conversation; create it lazily on first use.
        conv_id = cfg._default_convs.get(face_id)
        if not conv_id:
            conv_id = await _create_conversation(session, cfg, f"mcp:{face_id}")
            cfg._default_convs[face_id] = conv_id
    async with session.ws_connect(
        f"{cfg.gateway}/ws/chat", headers=cfg.headers
    ) as ws:
        await ws.send_json(
            {
                "type": "message",
                "conversation_id": conv_id,
                "content": message,
                "face_id": face_id,
            }
        )
        return await _consume_stream(ws)


async def _chat_turn(
    cfg: MCPConfig,
    message: str,
    face_id: str,
    conversation_id: str,
    session=None,
) -> str:
    """Run ONE chat turn through the gateway WS and return the reply text.

    Bounded by a wall-clock ceiling (cfg.turn_timeout) so a wedged-but-alive
    gateway turn can never hang the calling agent forever. Gateway/transport
    failures are surfaced as a legible ``[error:...]`` string, not a raised
    exception. *session* is injectable for tests.
    """
    own = session is None
    if own:
        session = aiohttp.ClientSession(timeout=_WS_TIMEOUT)
    try:
        return await asyncio.wait_for(
            _do_chat_turn(cfg, session, message, face_id, conversation_id),
            timeout=cfg.turn_timeout,
        )
    except asyncio.TimeoutError:
        return "[error:timeout] gateway did not complete the turn in time"
    except _UpstreamError as exc:
        code = {401: "unauthorized", 403: "forbidden"}.get(exc.status, "upstream")
        return f"[error:{code}] {exc}"
    except (aiohttp.ClientError, RuntimeError) as exc:
        return f"[error:upstream] {exc}"
    finally:
        if own:
            await session.close()


# ── Tool implementations (plain async; the @mcp.tool wrappers delegate here) ──

async def chat_with_face_impl(
    cfg: MCPConfig,
    message: str,
    face_id: str = "assistant",
    conversation_id: str = "",
    session=None,
) -> str:
    if not cfg.face_allowed(face_id):
        # Reject locally WITHOUT an upstream call — the requested face is outside
        # this token's allow-list.
        return f"[error:forbidden] face '{face_id}' is not in this token's allow-list"
    return await _chat_turn(cfg, message, face_id, conversation_id, session=session)


async def run_council_impl(
    cfg: MCPConfig,
    task: str,
    conversation_id: str = "",
    session=None,
) -> str:
    if not cfg.council_allowed:
        return "[error:forbidden] this token may not run the council"
    return await _chat_turn(cfg, task, COUNCIL_FACE, conversation_id, session=session)


# ── Server assembly ──────────────────────────────────────────────────────────

def build_server(cfg: MCPConfig) -> FastMCP:
    """Build the FastMCP server, registering only the tools this token may use."""
    mcp = FastMCP("bobclaw")

    @mcp.tool()
    async def chat_with_face(
        message: str, face_id: str = "assistant", conversation_id: str = ""
    ) -> str:
        """Generate a reply with an authorized Bob face through the gateway.
        May call tools and persist conversation events; not read-only memory recall.
        Use bob_guide for product facts and direct memory tools for source evidence.

        Args:
            message: the user message to send.
            face_id: the BoBClaw face/persona (default "assistant"). Must be in
                this token's faces allow-list, if one is set.
            conversation_id: continue a specific conversation; omit to continue
                this session's default thread for the face (created on first use).
        """
        return await chat_with_face_impl(cfg, message, face_id, conversation_id)

    if cfg.council_allowed:

        @mcp.tool()
        async def run_council(task: str, conversation_id: str = "") -> str:
            """Run BoBClaw's multi-model council (council-max) on a task and
            return its deliberated answer.

            Args:
                task: the problem/question for the council to deliberate.
                conversation_id: continue a specific conversation; omit to
                    continue this session's default council thread.
            """
            return await run_council_impl(cfg, task, conversation_id)

    # ── Action tools (v0.1, 2026-09-30): read/inspect + slug-staleness ────────
    # Thin REST proxies to the gateway (which enforces the agent-token route
    # allow-list server-side). No secrets in any response. These give MCP
    # clients what chat alone couldn't: enumerate the face roster, see backend
    # availability, check pinned model slugs against the catalog, and read the
    # server clock. All read-only — Bob's own write surface (projects, teams,
    # memory writes via chat turns) stays behind the existing chat/approval
    # gates. Scope semantics: any agent token accepted by the gateway gets
    # these (they leak no secrets and mutate nothing); the per-face filter
    # above still governs chat_with_face.

    async def _gw_json(method: str, path: str, params: dict | None = None, json_body: dict | None = None, timeout: int = 30) -> str:
        """One authenticated gateway REST call, returned as JSON text.
        Transport failures surface as an in-band ``[error:...]`` string —
        same legible-failure contract as the chat proxy."""
        url = f"{cfg.gateway}{path}"
        headers = cfg.headers
        from core.execution.models import wire_integers
        if json_body and json_body.get("contract_version") == 2:
            json_body = wire_integers(json_body)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.request(method, url, headers=headers, params=params, json=json_body) as r:
                    body = await r.text()
                    if r.status >= 400:
                        return f"[error:http_{r.status}] {body[:500]}"
                    return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return f"[error:upstream] {type(exc).__name__}: {exc}"

    from core.mcp.workflows import register as register_workflows
    register_workflows(mcp, _gw_json)
    from core.mcp.inspection import register as register_inspection
    register_inspection(mcp, _gw_json, cfg.face_allowed)

    @mcp.tool()
    async def bob_guide(topic: str = "index", face_id: str = "") -> str:
        """Read one small Bob guide topic, no model call or memory write. Start with index.

        Use agents/tools/limits before delegation, permissions before work and
        troubleshooting after failure. face_id selects a token-authorized face's
        tools; the gateway independently filters all visible face data.
        """
        if face_id and not cfg.face_allowed(face_id):
            return "[error:forbidden] face outside guide scope"
        return await _gw_json("GET", "/bob-guide", params={"topic": topic, "face_id": face_id})

    # FastMCP's resource decorator uses the same authenticated proxy as the tool.
    @mcp.resource("bob://guide/{topic}")
    async def guide_resource(topic: str) -> str:
        """Scoped on-demand Bob guide topic (index lists topics)."""
        return await bob_guide(topic)

    @mcp.tool()
    async def bob_memory_status() -> str:
        """Central server memory coverage, excluded sources and per-kind counts. NOT the client's local cache."""
        return await _gw_json("GET", "/agent-memory/status")

    @mcp.tool()
    async def bob_memory_search(query: str, limit: int = 10, offset: int = 0) -> str:
        """Search authorized central memory directly, no chat face required. Returns source IDs, excerpts and coverage; empty query lists records. Follow pagination, read full sources before claiming evidence."""
        return await _gw_json("POST", "/agent-memory/search", json_body={"query": query, "limit": limit, "offset": offset})

    @mcp.tool()
    async def bob_memory_read(source_id: str, offset: int = 0, max_chars: int = 6000) -> str:
        """Read an authorized central source by its exact returned ID; follow next_offset if truncated. Foreign sources are denied by server."""
        return await _gw_json("POST", "/agent-memory/read", json_body={"source_id": source_id, "offset": offset, "max_chars": max_chars})

    @mcp.tool()
    async def bob_memory_save(content: str, category: str = "fact") -> str:
        """Append a durable fact/preference/project/note to the authorized owner's central family memory. Owner inferred by server, not client. Read back returned source_id to verify; local caches are not authority."""
        return await _gw_json("POST", "/agent-memory/save", json_body={"content": content, "category": category})

    # ── Work-lane READ-ONLY tools (personal may look into work) ────────────────
    # Registered only for a token that reads personal memory. There is deliberately
    # no work-lane WRITE tool here: the personal surface is read-only on the work
    # lane, and the gateway rejects a save from a non-lane token with 403 anyway.
    if cfg.work_read_allowed:
        @mcp.tool()
        async def bob_work_search(query: str, limit: int = 10, offset: int = 0) -> str:
            """Search the SPS work lane READ-ONLY (semantic over sps__2560 plus literal hits). Returns work-lane source IDs and excerpts. This never writes and never touches personal memory."""
            return await _gw_json("POST", "/work-memory/search",
                                  json_body={"query": query, "limit": limit, "offset": offset},
                                  timeout=180)

        @mcp.tool()
        async def bob_work_read(source_id: str, offset: int = 0, max_chars: int = 6000) -> str:
            """Read one SPS work-lane source by the exact ID a work search returned (sps:note:…, sps:chunk:…, sps:doc:…). READ-ONLY; follow next_offset when truncated."""
            return await _gw_json("POST", "/work-memory/read",
                                  json_body={"source_id": source_id, "offset": offset,
                                             "max_chars": max_chars},
                                  timeout=60)

    @mcp.tool()
    async def list_agent_services() -> str:
        """Discover Bob model-faces, callable teams, limits and blocked seats. No chat persona required."""
        return await _gw_json("GET", "/agent-services/catalog", params={"contract_version": 2})

    if cfg.face_allowed("researcher"):
        @mcp.tool()
        async def list_saved_teams(cursor: str = "", limit: int = 20) -> str:
            """v2 server-owned team versions and computed eligibility. No deployment roster import."""
            return await _gw_json("GET", "/teams", params={"contract_version": 2, "cursor": cursor, "limit": limit})

        @mcp.tool()
        async def save_team(definition: dict, expected_revision: int | None, request_key: str) -> str:
            """Save a v2 team, without starting it. Null revision creates only. Reuse request_key after disconnect; changed payload conflicts."""
            return await _gw_json("POST", "/teams", json_body={"contract_version": 2, "definition": definition,
                                  "expected_revision": expected_revision, "request_key": request_key})

        @mcp.tool()
        async def start_team_run_v2(team: str, team_revision: int, task: str, request_key: str,
                                    start_calls: int = 10, wall_mode: str = "fixed", wall_seconds: int | None = 3600,
                                    project_id: str = "", conversation_id: str = "",
                                    evidence_ids: list[str] | None = None, acceptance: str | dict = "", intent_id: str = "") -> str:
            """Durable supplied-evidence run (four-step or agent-loop/v1). Loop requires acceptance.criteria with fixed checks. Propose a fitting start_calls; owner can override. Above 200 needs a stored owner grant. Accepted is not completed; no self-grant or fallback."""
            body = {"contract_version": 2, "team": team, "team_revision": team_revision, "task": task,
                    "request_key": request_key, "budget": {"start_calls": start_calls, "wall_mode": wall_mode,
                                                           "wall_seconds": wall_seconds}}
            if project_id:
                body["project_id"] = project_id
            if conversation_id:
                body["conversation_id"] = conversation_id
            if evidence_ids is not None:
                body["evidence_ids"] = evidence_ids
            if acceptance:
                body["acceptance"] = acceptance
            if intent_id:
                body["intent_id"] = intent_id
            return await _gw_json("POST", "/agent-services/runs", json_body=body)

        @mcp.tool()
        async def get_team_run_v2(run_id: str, after_event_seq: int = 0, receipt_cursor: int = 0) -> str:
            """Read durable progress/partial receipts. Unknown dispatched calls stay charged. No independent verification claim."""
            params = {"contract_version": 2, "after_event_seq": after_event_seq}
            if receipt_cursor:
                params["receipt_cursor"] = receipt_cursor
            return await _gw_json("GET", _run_path(run_id), params=params)

        @mcp.tool()
        async def prepare_execution_grant(request_key: str, request: dict | None = None,
                                          run_id: str = "", expected_revision: int | None = None) -> str:
            """Propose owner review only; creates a durable admission intent without starting work or granting calls. No issue/confirm authority is exposed to agents."""
            body = {"contract_version": 2, "request_key": request_key}
            if request is not None:
                body["request"] = request
            if run_id:
                body.update(run_id=run_id, expected_revision=expected_revision)
            return await _gw_json("POST", "/agent-services/grant-proposals", json_body=body)

        @mcp.tool()
        async def resume_team_run(run_id: str, expected_revision: int, request_key: str, budget: dict | None = None) -> str:
            """Human owner only. Resume a budget/no-progress/wall pause without widening authority. Agent credentials are denied. Reuse the exact request key on reconnect."""
            body = {"contract_version": 2, "expected_revision": expected_revision, "request_key": request_key}
            if budget is not None:
                body["budget"] = budget
            return await _gw_json("POST", _run_path(run_id) + "/resume", json_body=body)

        @mcp.tool()
        async def set_project_call_budget(project_id: str, call_limit: int, expected_revision: int | None, request_key: str) -> str:
            """Human owner credential required. Set an optional total project call pool, never a daily cap. Agents cannot grant themselves spending authority. Null revision creates only."""
            return await _gw_json("POST", "/agent-services/budgets", json_body={"contract_version": 2,
                                  "project_id": project_id, "call_limit": call_limit,
                                  "expected_revision": expected_revision, "request_key": request_key})

        @mcp.tool()
        async def list_team_runs(cursor: str = "", limit: int = 20, project_id: str = "", status: str = "") -> str:
            """v2 server-authoritative list across authorized clients; excludes the legacy portal ledger."""
            params = {"contract_version": 2, "cursor": cursor, "limit": limit}
            if project_id:
                params["project_id"] = project_id
            if status:
                params["status"] = status
            return await _gw_json("GET", "/agent-services/runs", params=params)

        @mcp.tool()
        async def cancel_team_run_v2(run_id: str, request_key: str) -> str:
            """Persist cancellation intent using the same key on retry; inspect returned status for terminal acknowledgement."""
            return await _gw_json("POST", _run_path(run_id) + "/cancel",
                                  json_body={"contract_version": 2, "request_key": request_key})

        @mcp.tool()
        async def start_team_run(team: str, task: str, conversation_id: str = "", project_id: str = "", require_bulk: bool = False, worker: str = "") -> str:
            """Legacy v1: exactly four calls, at most 900 seconds, read-only. Accepted is not completed. Use start_team_run_v2 for saved configured teams; coding and bulk stay blocked."""
            body = {"team": team, "task": task, "max_model_calls": 4,
                    "max_seconds": 900, "require_bulk": require_bulk}
            if worker:
                body["worker"] = worker
            if conversation_id:
                body["conversation_id"] = conversation_id
            if project_id:
                body["project_id"] = project_id
            return await _gw_json("POST", "/agent-services/runs", json_body=body)

        def _run_path(run_id: str) -> str:
            import uuid
            return "/agent-services/runs/" + str(uuid.UUID(run_id))

        @mcp.tool()
        async def get_team_run(run_id: str) -> str:
            """Read owned team progress/results and per-role receipts. Completion does not imply independent verification."""
            return await _gw_json("GET", _run_path(run_id))

        @mcp.tool()
        async def message_team_manager(run_id: str, message: str) -> str:
            """Talk to the manager of a finished team run in its Bob conversation. Does not secretly launch more workers."""
            return await _gw_json("POST", _run_path(run_id) + "/messages", json_body={"content": message}, timeout=600)

        @mcp.tool()
        async def cancel_team_run(run_id: str) -> str:
            """Cancel an owned active team run and read its terminal status."""
            return await _gw_json("POST", _run_path(run_id) + "/cancel")

    @mcp.tool()
    async def list_faces() -> str:
        """Discover face IDs/backends from the gateway; use bob_guide agents/tools for scoped roles and effective tools.

        Read-only. Returns each face's id so chat_with_face calls can be aimed;
        does not include system prompts or postures.
        """
        return await _gw_json("GET", "/faces")

    @mcp.tool()
    async def list_backends() -> str:
        """List every backend Bob's router knows, with kind and availability.

        Read-only. Uses the unified catalog (router view): backend name,
        kind (cloud/cli/local), available flag (key-present for cloud, binary
        resolves for CLI, reachable server for local), configured default
        model id where applicable, and per-worker cost/fan-out caps.
        """
        return await _gw_json("GET", "/backends/catalog")

    @mcp.tool()
    async def get_server_time() -> str:
        """Bob server time (ISO-8601 UTC) and service port map. Read-only."""
        return await _gw_json("GET", "/system/ports")

    @mcp.tool()
    async def check_model_slugs() -> str:
        """Check every face's pinned model slug for staleness.

        Compares each face profile's pinned model id (codex_posture.model,
        pi_posture.model, agy_posture.model — from the full face profile,
        not the summary list) against the unified backends catalog
        (/backends/catalog — the router view, so CLI backends are judged
        against their real entries, not flagged phantom-missing). Reports
        per-face: pinned slug, backend, whether the backend exists and is
        available, and the backend's configured default model. Purely
        diagnostic — updating a pin is a human-approved config edit, never
        done here.
        """
        faces_raw = await _gw_json("GET", "/faces")
        models_raw = await _gw_json("GET", "/backends/catalog")
        try:
            faces = json.loads(faces_raw)
            models = json.loads(models_raw)
        except ValueError:
            return "[error:bad_upstream] gateway returned non-JSON"
        catalog: dict[str, dict] = {m.get("backend"): m for m in models if isinstance(m, dict)}
        out = []
        for f in faces if isinstance(faces, list) else []:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            fid = f["id"]
            # The summary list carries no postures — pull the full profile for
            # the pinned-model check (one small GET per face, ~20 faces).
            detail_raw = await _gw_json("GET", f"/faces/{fid}")
            try:
                detail = json.loads(detail_raw)
            except ValueError:
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            # Pinned slug sources, in priority order: the CLI postures carry an
            # explicit model; preferred_backend names the backend itself.
            posture_models = [
                (posture_name, (detail.get(posture_name) or {}).get("model"))
                for posture_name in ("codex_posture", "pi_posture", "agy_posture")
                if isinstance(detail.get(posture_name), dict)
            ]
            backend = detail.get("preferred_backend") or f.get("preferred_backend") or "auto"
            entry = catalog.get(backend)
            pinned = [
                {"posture": pn, "model": pm}
                for pn, pm in posture_models
                if pm
            ]
            row = {
                "face": fid,
                "backend": backend,
                "backend_exists": entry is not None,
                "backend_available": bool(entry.get("available")) if entry else False,
                "backend_default_model": entry.get("model") if entry else None,
                "pinned_models": pinned,
            }
            # A pinned slug is stale when its backend is wired but unavailable,
            # or when the backend lists models and the slug is absent from them.
            stale = False
            notes: list[str] = []
            if entry is None and backend != "auto":
                notes.append(f"backend '{backend}' not in catalog")
            if entry and not entry.get("available"):
                stale = True
                notes.append("backend wired but unavailable")
            offered = (entry or {}).get("models") or []
            for pn, pm in posture_models:
                if pm and offered and pm not in offered:
                    # Only flag when the backend actually enumerates models.
                    stale = True
                    notes.append(f"{pn}.model '{pm}' not offered by backend")
            row["stale"] = stale
            row["notes"] = notes
            out.append(row)
        return json.dumps({"checked": len(out), "faces": out}, indent=2)

    return mcp
