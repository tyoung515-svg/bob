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
    ):
        self.token = token
        self.gateway = gateway.rstrip("/")
        self.faces = faces
        self.turn_timeout = turn_timeout
        # Per-face default conversation, lazily created and reused so repeated
        # tool calls form a continuous thread (one stdio server = one agent).
        self._default_convs: dict[str, str] = {}

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def face_allowed(self, face_id: str) -> bool:
        # Empty allow-list ⇒ unrestricted (the admin who minted the token did not
        # constrain faces). A non-empty list is a strict allow-list.
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
        token=token, gateway=gateway, faces=_decode_faces(token), turn_timeout=turn_timeout
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
        """Chat with a specific BoBClaw face/persona and return its reply.

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

    return mcp
