"""Offline pre-extraction oracle. Run each surface in a fresh Python process.

Usage: python tests/roster_golden/capture.py core|gateway [--write]
Without --write prints canonical JSON; --write refuses to replace a golden.
Handlers are invoked directly: no HTTP listener, discovery, inference or DB.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def decoded(response):
    assert response.status == 200, response.text
    return json.loads(response.text)


class Request(dict):
    def __init__(self, app=None, *, face_id=None, query=None):
        super().__init__(user={"sub": "golden", "faces": ["researcher"]})
        self.app = app
        self.headers = {}
        self.method = "GET"
        self.query = query or {}
        self.match_info = {"face_id": face_id}


async def fallback_capture():
    """Exercise every implicit execute fallback, including explicit precedence."""
    from aiohttp import ClientResponseError
    from core.nodes import execute as ex
    from core.backends.opencode_pool import NoOpenCodeAvailable
    from core.backends.claude_code import ClaudeCodeThrottled
    from core.backends.agy_code import AgyThrottled
    from core.backends.codex_code import CodexThrottled
    from core.backends.pi_code import PiError

    cases = [
        ("opencode_unavailable", "opencode_serve", "core.backends.opencode_pool._pool.dispatch", NoOpenCodeAvailable("offline")),
        ("claude_throttled", "claude_code", "core.backends.claude_code.ClaudeCodeClient", ClaudeCodeThrottled("throttle")),
        ("agy_throttled", "agy_code", "core.backends.agy_code.AntigravityClient", AgyThrottled("throttle")),
        ("codex_throttled", "codex_code", "core.backends.codex_code.CodexCodeClient", CodexThrottled("throttle")),
        ("kimi_429", "kimi_code", None, ClientResponseError(MagicMock(), (), status=429)),
        ("pi_429", "pi_code", None, ClientResponseError(MagicMock(), (), status=429)),
        ("pi_error", "pi_code", None, PiError("offline")),
    ]
    output = {}
    for name, backend, target, error in cases:
        output[name] = {}
        for escalation in (None, "deepseek_v4_flash"):
            calls = []

            async def send(messages, selected, *args):
                calls.append(selected)
                if target is None and len(calls) == 1:
                    raise error
                return "fixture answer"

            with ExitStack() as stack:
                stack.enter_context(patch.object(ex, "_is_tool_enabled", return_value=False))
                stack.enter_context(patch.object(ex, "_face_spawn_posture", side_effect=lambda p, s: p))
                stack.enter_context(patch.object(ex, "_get_stream_writer", return_value=None))
                stack.enter_context(patch.object(ex, "_append_agent_turn_event", new=AsyncMock()))
                stack.enter_context(patch.object(ex, "_check_escalation_pin", new=AsyncMock(return_value=None)))
                stack.enter_context(patch.object(ex, "_pin_escalation", new=AsyncMock()))
                stack.enter_context(patch.object(ex, "_stream_and_collect", side_effect=send))
                if target:
                    if target.endswith("dispatch"):
                        stack.enter_context(patch(target, new=AsyncMock(side_effect=error)))
                    else:
                        client = stack.enter_context(patch(target)).return_value
                        client.chat = AsyncMock(side_effect=error)
                result = await ex.execute_node({
                    "task": "hello", "backend": backend, "messages": [],
                    "escalation_backend": escalation, "agy_posture": {},
                    "codex_posture": {}, "cc_posture": {},
                })
                assert result.get("error") is None, (name, result)
                output[name]["implicit" if escalation is None else "explicit"] = calls
    return output


async def core_capture():
    from api import server
    from core import teams
    from core.config import config
    import core.config as cfg
    from core.faces.registry import FaceRegistry
    from core.mcp_tools.profile_routing import class_to_face
    from core.nodes.route import _select_face, _build_council_spec
    from tools import bob_bridge

    registry = FaceRegistry()
    router = SimpleNamespace(discover=AsyncMock(return_value=[SimpleNamespace(
        name="fixture-local", url="http://127.0.0.1:1", models=["chat-b", "chat-a"])]))
    app = server.build_app(faces=registry, router=router)
    if "--check-registry-identity" in sys.argv:
        from core.faces.registry import get_registry, get_default_registry
        registry = get_registry()
        app = server.build_app(router=router)
        assert app[server.FACES_KEY] is registry is get_default_registry()
        assert bob_bridge.build_app()[bob_bridge.FACES_KEY] is registry
        # Explicit injection, including an empty registry, stays authoritative.
        injected = FaceRegistry.__new__(FaceRegistry)
        injected._faces = {}
        assert server.build_app(faces=injected)[server.FACES_KEY] is injected
        assert bob_bridge.build_app(faces=injected)[bob_bridge.FACES_KEY] is injected
    request = Request(app)
    with patch.object(server.shutil, "which", return_value="/fixture/bin/tool"), \
         patch.object(teams, "_load_custom_teams", return_value={}):
        out = {}
        for path, handler in [
            ("/api/faces", server.list_faces),
            ("/api/backends", server.list_backends_endpoint),
            ("/api/backends/catalog", server.backends_catalog),
            ("/api/models/available", server.list_available_models),
            ("/api/routing-view", server.routing_view),
        ]:
            out[path] = decoded(await handler(request))
        out["face_details"] = {f.id: decoded(await server.get_face(Request(app, face_id=f.id)))
                               for f in registry.all_faces()}
        out["teams"] = teams.list_teams()
        out["team_resolution"] = {name: decoded(await server.routing_view(
            Request(app, query={"team": name}))) for name in teams.known_teams()}
        states = [{"dispatch_subtask": {}}, *({"phase": p} for p in ("dispatch", "execute", "build", "worker")),
                  {"task": "plan a function"}, {"task": "plan a holiday"}, {"task": "hello"}]
        out["intent_faces"] = [await _select_face(s) for s in states]
        out["class_faces"] = {c: class_to_face(c) for c in (
            "worker", "route_cheap", "plan_code", "synth_mid", "plan_concept", "synth_deep", "unknown", "")}
        with patch.object(bob_bridge.time, "time", return_value=1700000000):
            out["bridge_models"] = decoded(await bob_bridge.models(Request(bob_bridge.build_app(faces=registry))))
        out["council"] = {"spec": _build_council_spec({}), "backends": cfg.COUNCIL_SEAT_BACKENDS,
                          "ground_backend": cfg.COUNCIL_GROUND_BACKEND,
                          "gate_critic": config.GATE_CRITIC_BACKEND,
                          "fallback_critic": config.CRITIC_FALLBACK_BACKEND}
        out["execute_fallbacks"] = await fallback_capture()
    return out


async def gateway_capture():
    from routers import agent_services as svc
    with patch.object(svc, "local_status", new=AsyncMock(return_value={
        "available": False, "model": "qwen3.8-flash-next-iq3_xxs", "reason": "fixture_offline"})):
        return {"/agent-services/catalog": decoded(await svc.catalog(Request())),
                "/v1/models": decoded(await svc.models(Request()))}


def capture(surface, *, env_overrides=None):
    # Fixed, fake configuration; never read repository dotenv or host credentials.
    env = {"PATH": "/fixture/bin", "HOME": "/fixture/home", "MEMORY_ENABLED": "false",
           "MEMORY_WRITER_ENABLED": "false", "BOB_UNAVAILABLE_BACKENDS": "kimi_code",
           "ANTHROPIC_API_KEY": "fixture", "DEEPSEEK_API_KEY": "fixture",
           "KIMI_API_KEY": "fixture", "MINIMAX_API_KEY": "fixture"}
    if "BOB_ROSTER_FILE" in os.environ:
        env["BOB_ROSTER_FILE"] = os.environ["BOB_ROSTER_FILE"]
    env.update(env_overrides or {})
    sys.path[:0] = [str(ROOT / ("bobclaw-core" if surface == "core" else "bobclaw-gateway")),
                    str(ROOT / "bobclaw-core")]
    # Windows creates its event-loop wakeup socketpair using connect(). Create
    # that local runtime machinery before arming the deny-all network guard.
    with asyncio.Runner() as runner:
        runner.get_loop()
        return _guarded_capture(runner, surface, env)


def _guarded_capture(runner, surface, env):
    with patch.dict(os.environ, env, clear=True), \
         patch("dotenv.load_dotenv", return_value=False), \
         patch("tiktoken.get_encoding", return_value=SimpleNamespace(
             encode=lambda text, **kw: list(text.encode("utf-8")),
             decode=lambda tokens, **kw: bytes(tokens).decode("utf-8", errors="replace"))), \
         patch("platform.uname", return_value=SimpleNamespace(
             system="Linux", node="fixture", release="fixture", version="fixture",
             machine="x86_64", processor="x86_64")), \
         patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
         patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")), \
         patch("subprocess.Popen", side_effect=AssertionError("external process forbidden")):
        return runner.run(core_capture() if surface == "core" else gateway_capture())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    surface = sys.argv[1]
    assert surface in {"core", "gateway"}
    overrides = json.loads(sys.argv[sys.argv.index("--env-json") + 1]) if "--env-json" in sys.argv else None
    data = json.dumps(capture(surface, env_overrides=overrides), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if "--write" in sys.argv:
        with (HERE / (surface + ".json")).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
        print(f"Captured {surface}.json")
    else:
        print(data, end="")
