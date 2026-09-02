# bobclaw-telegram — Telegram chat-only pilot (phase 3A)

A minimal Telegram front-end for BoBClaw: **single-operator, DMs only,
notify-only approvals**. Uses python-telegram-bot (PTB) long-polling — no
public URL or webhook needed.

## Status

All three 3A tasks are landed:

- **Task 1** — package skeleton, fail-closed allowlist gate, gateway auth
  (cached token pair + `/auth/refresh` rotation, 0600 cache).
- **Task 2** — message flow: DM text is batched (~0.6s debounce) into one
  BoB turn per chat over `/ws/chat` with a per-chat conversation
  (find-or-created via `POST /conversations`, mapped in `.data/sessions.db`
  — gitignored). Replies stream with edit-in-place (~1 edit / 0.8s),
  fence-aware 4096-char chunking, MarkdownV2 with plain-text fallback, and a
  typing indicator. `/stop` sends `stop_generation` with the chat's
  conversation id. Last-processed `update_id` is persisted so replays after
  a restart are skipped.
- **Task 3** — approvals relay (notify-only), per-chat rate limits, systemd
  user unit, this README's ops section.

## Setup

1. Create the bot with **@BotFather** (`/newbot`) and copy the token.
2. Get your **numeric** Telegram user id (e.g. message **@userinfobot**).
   Usernames are never accepted.
3. Put both in `.secrets/bobclaw.env` at the repo root (alongside the
   existing `BOBCLAW_PASSWORD` / `TOTP_SECRET` the bot logs in with):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_ALLOWED_USERS=123456789
   ```

4. Install the deps into the dev venv (PTB is new for this package):

   ```bash
   .venv/bin/pip install -r bobclaw-telegram/requirements.txt
   ```

## Configuration

Values come from the process environment first, then
`.secrets/bobclaw.env` at the repo root:

| Var | Required | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | BotFather token |
| `TELEGRAM_ALLOWED_USERS` | yes | Comma-separated **numeric** Telegram user ids (fail-closed: empty/missing/non-numeric = fatal startup error). Usernames are never accepted. |
| `BOBCLAW_GATEWAY` | no | Gateway address, default `127.0.0.1:7836` |

Gateway auth reuses the gateway's own credentials (`BOBCLAW_PASSWORD` +
`TOTP_SECRET` from `.secrets/bobclaw.env`): `POST /auth/login` once, then the
token pair is cached in `.secrets/telegram-refresh.json` (mode `0600`, set at
file creation) and rotated via `POST /auth/refresh`. The bot never mints its
own tokens and never touches the TOTP seed beyond the login call.

## Approvals — notify-only

A relay polls `GET /approvals?status=pending` every ~15s and sends each
allowlisted DM one message per **new** pending approval (diffed against the
ids seen so far):

> ⏳ Approval pending: \<kind\> — \<summary\> — decide in the TUI

There are **no inline buttons and no remote approve/deny** — decisions stay
in the TUI (the gateway `/ui` is gone, so the TUI is the only human-reachable
approvals surface). If a chat turn parks on an approval wait, the bot adds a
note to that chat ("waiting on an approval — decide in the TUI"); the stream
stays open and completes once the approval is decided elsewhere. Seen-ids
state is in-memory, so after a restart everything still pending is relayed
once.

## Rate limits

Per chat (one turn = one batched flush, before any gateway traffic):

- **10 turns/minute** (sliding window) — over it: *"Easy there — that's a
  lot of turns…"*
- **200 turns/UTC-day** — over it: *"Daily turn limit reached (200)…"*

Refused turns don't consume budget. The limiter is in-memory: a restart
resets the windows.

## Run

```bash
# from the repo root, with the dev venv:
PYTHONPATH=bobclaw-telegram .venv/bin/python -m bobclaw_telegram
```

Missing/invalid config exits with a clear message before any network I/O.

## Ops (systemd user unit)

A user-mode unit ships at `scripts/bobclaw-telegram.service` (not installed
by the repo — install when ready):

```bash
mkdir -p ~/.config/systemd/user
cp scripts/bobclaw-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bobclaw-telegram.service
```

Then:

```bash
systemctl --user status bobclaw-telegram.service   # status check
systemctl --user stop bobclaw-telegram.service     # stop (start: ... start)
journalctl --user -u bobclaw-telegram.service -f   # logs
```

The unit runs the dev venv's `python -m bobclaw_telegram` with
`PYTHONPATH=bobclaw-telegram`, restarts `on-failure` (5s), and reads secrets
from `.secrets/bobclaw.env` — never inline secrets in the unit. The paths in
the unit are absolute to this checkout; adjust if the tree moves.

## Tests

```bash
cd bobclaw-telegram
PYTHONPATH=. ../.venv/bin/python -m pytest -q
```

## Security posture

- **Single-operator**: only the numeric user ids in
  `TELEGRAM_ALLOWED_USERS` are served. Anyone else gets the fixed reply
  `not authorized` and a warning log line; the gate compares the integer id
  Telegram asserts — never usernames, never string coercion.
- **Notify-only approvals (3A)**: the bot relays "approval pending" notices
  but cannot decide them; all decisions happen in the TUI.
- **DMs only**: group chats, media, voice, and pairing are out of scope for
  the pilot.
- Gateway tokens live only in `.secrets/telegram-refresh.json` (`0600`).

## Privacy note

This bot sends your chat messages **off-machine to Telegram's servers**
(that is how Telegram bots work — the Bot API is polled over TLS, but message
content transits Telegram). Only chat with it about things you are
comfortable routing through Telegram, same as any Telegram conversation.
