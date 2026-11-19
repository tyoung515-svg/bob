#!/usr/bin/env python3
"""
BoBClaw PC CLI — talk to the running stack through the gateway (the real app path).

Exercises: gateway login (password + TOTP from .secrets) -> JWT -> create
conversation -> WebSocket /ws/chat -> switch backend -> stream replies.

Usage (from repo root, with the stack up):
    ../.venv/Scripts/python.exe scripts/bobclaw_cli.py                 # interactive REPL
    ../.venv/Scripts/python.exe scripts/bobclaw_cli.py -m "hello"      # one-shot
    ../.venv/Scripts/python.exe scripts/bobclaw_cli.py --backend local # use llama.cpp/local instead of cloud

Credentials are read from .secrets/bobclaw.env (BOBCLAW_PASSWORD, TOTP_SECRET).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pyotp
from dotenv import dotenv_values

# Windows consoles default to cp1252; model output is full of em-dashes, ≥, emoji.
# Force UTF-8 so streaming never crashes mid-reply on an un-encodable char.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_SECRETS = Path(__file__).resolve().parents[1] / ".secrets" / "bobclaw.env"
_TOKEN_CACHE = Path(__file__).resolve().parents[1] / ".secrets" / "cli-token.txt"


async def _get_token(s, gateway: str, password: str, totp_secret: str) -> str:
    """Return a valid access token, reusing the cached one when possible.

    TOTP replay protection rejects a second login within the same 30s
    timestep, so back-to-back CLI invocations MUST reuse tokens instead of
    logging in every run. Cache validity is checked with a cheap authed GET.
    """
    if _TOKEN_CACHE.exists():
        token = _TOKEN_CACHE.read_text().strip()
        if token:
            try:
                async with s.get(
                    f"{gateway}/conversations",
                    headers={"Authorization": f"Bearer {token}"},
                ) as r:
                    if r.status == 200:
                        return token
            except aiohttp.ClientError:
                pass
    async with s.post(
        f"{gateway}/auth/login",
        json={"password": password, "totp_code": pyotp.TOTP(totp_secret).now()},
    ) as r:
        if r.status != 200:
            raise SystemExit(f"login failed {r.status}: {await r.text()}")
        token = (await r.json())["access_token"]
    try:
        _TOKEN_CACHE.write_text(token)
    except OSError:
        pass
    return token


async def _stream(ws, conv_id: str, text: str, face: str) -> None:
    await ws.send_json(
        {"type": "message", "conversation_id": conv_id, "content": text, "face_id": face}
    )
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                print("\n[connection closed]")
                break
            continue
        evt = json.loads(msg.data)
        t = evt.get("type")
        if t == "chunk":
            print(evt.get("content", ""), end="", flush=True)
        elif t == "message_complete":
            print(
                f"\n[done out={evt.get('tokens_out')} tok, {evt.get('elapsed_ms')} ms]"
            )
            break
        elif t == "error":
            print(f"\n[error:{evt.get('code')}] {evt.get('message')}")
            break
        elif t == "generation_stopped":
            print(f"\n[generation stopped: {evt.get('code') or 'stopped'}]")
            break
        elif t in ("model_switched", "face_switched"):
            continue


async def main() -> None:
    ap = argparse.ArgumentParser(description="BoBClaw PC CLI client")
    ap.add_argument("-m", "--message", help="one-shot message (omit for interactive REPL)")
    ap.add_argument("--backend", default="deepseek_v4_flash",
                    help="backend: deepseek_v4_flash (default/cheap), minimax (senior reasoning), "
                         "kimi_code, gemini_flash, claude_api (Opus — reserved), local. "
                         "Omit --backend (or pass '') to let core face routing pick the backend.")
    ap.add_argument("--model", default="deepseek-v4-flash", help="model name passed to the backend. "
                         "'local' is mapped to None (no override, router picks the resident model).")
    ap.add_argument("--face", default="assistant", help="face/persona id")
    ap.add_argument("--gateway", default="http://localhost:7826")
    ap.add_argument("--conversation", default="", help="resume an existing conversation by id (skip creation)")
    args = ap.parse_args()
    if args.model and args.model.strip().lower() == "local":
        args.model = ""

    env = dotenv_values(_SECRETS)
    password = env.get("BOBCLAW_PASSWORD", "")
    totp_secret = env.get("TOTP_SECRET", "")
    if not password or not totp_secret:
        raise SystemExit(f"BOBCLAW_PASSWORD / TOTP_SECRET missing in {_SECRETS}")

    # No total cap + a generous per-read so a long CoCouncil restart turn (>5 min,
    # silent on the WS during grounding spawns) isn't cut at aiohttp's default 300s.
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_read=600)
    ) as s:
        # 1. auth — cached token first, fresh TOTP login only when needed
        token = await _get_token(s, args.gateway, password, totp_secret)
        headers = {"Authorization": f"Bearer {token}"}

        # 2. conversation — create new or resume existing
        conv_id = args.conversation.strip()
        if conv_id:
            async with s.get(
                f"{args.gateway}/conversations/{conv_id}", headers=headers
            ) as r:
                if r.status >= 400:
                    raise SystemExit(f"conversation {conv_id} not found or access denied")
            print(f"[resumed] conversation={conv_id} backend={args.backend}")
        else:
            async with s.post(
                f"{args.gateway}/conversations", json={"title": "PC CLI"}, headers=headers
            ) as r:
                if r.status >= 400:
                    raise SystemExit(f"create conversation failed {r.status}: {await r.text()}")
                conv_id = (await r.json())["id"]
            print(f"[connected] gateway={args.gateway} conversation={conv_id} backend={args.backend}")

        # 3. WebSocket chat
        async with s.ws_connect(f"{args.gateway}/ws/chat", headers=headers) as ws:
            if args.backend:
                # Pins are per-conversation now: switch_model requires the
                # conversation_id. Empty --backend = no pin, core face routing
                # picks the backend.
                await ws.send_json(
                    {
                        "type": "switch_model",
                        "model": args.model,
                        "backend": args.backend,
                        "conversation_id": conv_id,
                    }
                )
            if args.message:
                await _stream(ws, conv_id, args.message, args.face)
            else:
                print("Interactive chat. Empty line or Ctrl-C to quit.")
                loop = asyncio.get_event_loop()
                while True:
                    try:
                        text = await loop.run_in_executor(None, input, "\nyou> ")
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not text.strip():
                        break
                    await _stream(ws, conv_id, text, args.face)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
