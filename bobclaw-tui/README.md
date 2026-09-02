# bobclaw-tui — the flight cockpit (Lane 1c)

A Textual TUI that **watches the fleet/council live**, grouped per flight — the view plane
of the flight substrate. It renders the data plane (`/ws/monitor`), a flights table
(`/flights`), and an agent window (`/ws/chat`).

## Panes (tmux-style grid)

- **Flights** (top-left) + **Fleet** (mid-left) + **Council/agent log** (mid-right) — the
  live `/ws/monitor` stream, grouped per flight.
- **Routing · JOAT** (top-right) — polls `/routing-view`: active team, live-probe posture,
  faces → resolved backend (rerouted rows first, 🔧 = tool-capable).
- **Approvals · Gate** (top-right) — polls `/approvals?status=pending` and is **actionable**:
  this is the only human-reachable approvals surface (the gateway `/ui` was removed and the
  KMM app is deferred), so the always-human gate lives here. See *Approvals* below.
- **Bots · teammates** (right column, under Approvals) — polls `/agents` + `/faces`: one
  selectable row per teammate binding (avatar, display_name, slug, last-activity), with a
  `●` unread watermark tracked per slug in `.secrets/tui-state.json`. Enter binds the bot's
  canonical conversation and renders its history (the same path as the `/chats` picker).
- **Agent window** (bottom) — one interactive `/ws/chat` turn (see below). Long lines
  soft-wrap to the pane width (no horizontal scrolling).

## Layout (split for testability)

- `bobclaw_tui/monitor_state.py` — **pure reducer**: monitor frames → a per-flight
  fleet/council/cost model, grouped by `flight_id` (named "block of work" flights first,
  then ambient `chat:*` / `ambient` work). CI-tested (`tests/test_monitor_state.py`), no
  Textual needed.
- `bobclaw_tui/panels.py` — **pure formatters** for the REST-poll panes (Routing + Approvals).
  JSON → cockpit lines, no I/O; CI-tested (`tests/test_panels.py`).
- `bobclaw_tui/monitor_client.py` — a thin aiohttp `/ws/monitor` WS client (auto-reconnect)
  that feeds frames into the reducer.
- `bobclaw_tui/app.py` — the Textual app (Textual imported here only; optional dep). A thin
  renderer over the tested state (`monitor_state` + `panels`) — exercised by a live run /
  the Textual pilot harness, not a plain unit test (no TTY).

## Run

```powershell
# gateway must be up first: scripts/win/start-gateway.ps1
scripts/win/start-tui.ps1                 # logs in via the gateway /auth/login
scripts/win/start-tui.ps1 -Flight ms-5    # watch one flight
# or directly:
python -m bobclaw_tui --gateway 127.0.0.1:7836
```

The TUI logs in with `BOBCLAW_PASSWORD` + `TOTP_SECRET` from the gateway's
`.secrets/bobclaw.env` (`POST /auth/login`), caches the token pair in
`.secrets/tui-token.json` (mode 0600), and refreshes it via `POST /auth/refresh` when
the access token expires — no self-minted tokens. Pass `--token <jwt>` to override the
login flow (tests/manual use). The gateway address defaults to `127.0.0.1:7836` and can
be overridden with `--gateway` or `$BOBCLAW_GATEWAY`.

Textual is installed on first launch by `start-tui.ps1`.

## Agent window + slash commands

Type `/` in the agent line and a command menu **pulls up** (like other TUIs): ↑/↓ to
move, **Tab** to complete, **Enter** to run an arg-less command (or complete one that
needs an argument), **Esc** to dismiss, or click a row. A space past the command token
closes the menu. Anything without a leading `/` is a normal `/ws/chat` turn.

| Command | Effect |
|---|---|
| `/help` | list the commands in the log |
| `/bots` | focus the Bots pane (teammate roster; ↑/↓ + Enter binds the highlighted bot's chat) |
| `/bot <slug> [message]` | open a teammate's canonical chat (on first use for a teammate face, creates the binding via `POST /agents`); with a message, also sends it as a normal turn |
| `/profile <name>` | pin a profile on this conversation (WS `switch_profile`) |
| `/face <id>` | pin a face on this conversation (WS `switch_face`) |
| `/model <model> [backend]` | pin a model (+ optional backend) on this conversation (WS `switch_model`; no backend = back to auto routing) |
| `/council <prompt>` | convene the council (`council-max`) for this turn |
| `/flight <id>` | re-filter the monitor to one flight live (empty = all flights) |
| `/refresh` | refresh routing + approvals + flights now (also the `r` key) |
| `/clear` | clear the agent/council log |

## Approvals (the always-human gate)

The TUI is a human-authenticated surface, so it is where gate decisions happen (the old
`/ui` dashboard is gone). Two surfaces, one rule: **a decision is never a single keypress.**

- **Pending pane** — `j`/`k` move the selection (`>` marks the row), `a` / `d` start an
  approve / deny. While the message input has focus those letters are typed text instead —
  use the input-safe variants **Ctrl+↓ / Ctrl+↑** (select) and **Ctrl+y / Ctrl+n**
  (approve / deny), which fire from anywhere. Either way only a confirm line opens in the
  agent log; the decision fires (`POST /approvals/{id}/decide`) only when you then submit
  a literal `y` (`n` or **Esc** cancels). The outcome renders inline — including the
  `agent_resume` status, so a recorded decision whose agent replay failed is visible — and
  the pane refreshes.
- **In-chat prompts** — when a gated action parks mid-turn, the `approval_request` frame
  renders as a prompt in the agent log (action type + details summary). `y` / `n` sends the
  `approval_response` frame on the live turn socket and the turn resumes; **Esc** dismisses
  without deciding (the approval stays pending for the pane). While a prompt is open,
  ordinary text is swallowed — never sent as a turn.

## Fleet pane — two tiers (orchestration + workers)

BoB's fan-out is **flat today** (one orchestrator → N workers; hierarchical managers that
dispatch their own workers are a roadmap item), so there is no separate manager-agent
frame. The Fleet pane renders the orchestration tier as a per-flight **header** — the
dispatch wave marker (`fleet_start`: kind/wave/dispatched-N/backend), the reduce
(`fleet_join`: ok/total/failed), and for a council the synthesizer (`council_synth`) —
with the workers (and council seats) indented beneath it.

## Phasing

P1 (shell + monitor + agent window) and P2 (fleet/council panes wired to `/ws/monitor`,
grouped by `flight_id`) land here. P3 = this launcher + docs. **Routing + Approvals panes
wired** (`/routing-view` + `/approvals` polls) — the INTAKE §3.2 pane set is now complete.
The KMM Monitor section stays a separate later lane (do not couple).
