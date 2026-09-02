"""bobclaw-telegram — PTB long-polling runner (phase 3A).

Starts the bot, applies the allowlist gate to every inbound update, answers
``/ping`` with ``pong``, batches DM text into rate-limited BoB turns over
/ws/chat, and maps ``/stop`` to stop_generation. Non-allowlisted users get
the fixed refusal and are logged; group chats get a polite refusal (DMs only
in 3A). Alongside the handlers, an approvals relay polls the gateway's
``/approvals?status=pending`` every ~15s and notifies each allowlisted DM of
NEW pending approvals — notify-only, decisions stay in the TUI.

Run: ``python -m bobclaw_telegram`` (from the ``bobclaw-telegram/`` dir, with
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_ALLOWED_USERS`` in the environment or
``.secrets/bobclaw.env``). Missing/invalid config exits with a clear message.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .approvals import POLL_INTERVAL_S, ApprovalNotifier
from .bot import GatewayAdapter, build_handlers
from .config import Config, ConfigError, load_config
from .gateway_client import list_pending_approvals
from .session_map import SessionMap

log = logging.getLogger("bobclaw-telegram")


def build_app(config: Config):
    """Construct the PTB Application: gated handlers + the approvals relay."""
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    handlers = build_handlers(
        config=config,
        sessions=SessionMap(),
        gateway=GatewayAdapter(config.gateway),
    )

    # The approvals relay is a plain asyncio task on PTB's loop (no apscheduler
    # dependency for JobQueue). DM chat ids equal the allowlisted user ids.
    relay: dict = {}

    async def _start_relay(app) -> None:
        async def fetch() -> list[dict]:
            return await list_pending_approvals(config.gateway)

        async def send(chat_id: int, text: str) -> None:
            await app.bot.send_message(chat_id=chat_id, text=text)

        notifier = ApprovalNotifier(
            fetch=fetch, send=send, chat_ids=sorted(config.allowed_users)
        )
        relay["stop"] = asyncio.Event()
        relay["task"] = asyncio.ensure_future(notifier.loop(relay["stop"]))
        log.info("approvals relay polling every %.0fs", POLL_INTERVAL_S)

    async def _stop_relay(app) -> None:
        stop, task = relay.get("stop"), relay.get("task")
        if stop is not None:
            stop.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    app = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(_start_relay)
        .post_shutdown(_stop_relay)
        .build()
    )
    app.add_handler(CommandHandler("ping", handlers.ping))
    app.add_handler(CommandHandler("stop", handlers.stop))
    # DMs only (3A scope) — group messages are refused by the handlers, and this
    # filter keeps them from reaching the turn path at all (audit 3A).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handlers.text))
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # httpx logs full request URLs at INFO — which include the bot token in the
    # path. Clamp it (and PTB's HTTP chatter) to WARNING so tokens never hit logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"bobclaw-telegram: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    app = build_app(config)
    log.info(
        "starting long poll (gateway=%s, %d allowlisted user(s))",
        config.gateway,
        len(config.allowed_users),
    )
    app.run_polling()


if __name__ == "__main__":
    main()
