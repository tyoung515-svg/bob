# BoB Bridge operations runbook

The BoB bridge is a loopback-only OpenAI-compatible front for the core engine. It
routes every model whose name starts with `bob` through `POST /api/chat`, so the
normal L0 append, T0 projection, completion ledger, and cross-conversation recall
remain inside bob. Other model names are passed through only when
`BOB_BRIDGE_UPSTREAM` is configured.

Claude is intentionally outside this bridge's client scope.

## Contract and safety posture

- Listen address: fixed to `127.0.0.1`; default port `8902`.
- Health: `GET /health`.
- Models: `GET` or `POST /v1/models`.
- Hermes/Kimi wire: `POST /v1/chat/completions`.
- Codex wire: `POST /v1/responses` (a deliberately small compatibility surface).
- Bare `bob` selects the `assistant` face. `bob-<face-id>` selects that face.
- Only faces whose registry role is exactly `worker` are advertised or accepted.
  Planner/apex, critic, unknown, and legacy role-less faces are rejected with
  `503`. This prevents a bridge request from selecting a planner that dispatches
  back into Codex, Hermes, or Kimi and creating a recursion loop. In the current
  registry `builder-bob` is role-less, so the example name `bob-builder-bob` is
  deliberately not available until that face is explicitly classified as a
  worker.
- The bridge sends `pin_authoritative: true`, preventing bob's intent router from
  swapping an accepted worker face for a planner.
- `BOB_BRIDGE_MAX_HISTORY` defaults to 30. A bob request with more input messages
  is rejected and tells the operator to start a fresh CLI session.
- Logs contain UTC timestamp, lane, requested model, selected face, an opaque
  12-character session hash, bob-turn number, chunk count, latency, and status.
  They never contain message text, headers, or credentials. Turn counts are
  process-local and reset when the unit restarts.
- There is no bridge auth. Do not proxy or bind it beyond loopback. External
  clients belong on the authenticated gateway at `:7836`.

## Conversation strategy

The bridge ships transcript flattening, not last-user-only forwarding.

Evidence: core's `api/server.py` reads an optional `history` field from the
incoming payload and formats it into the initial state, but it never loads the
`messages` table. It also gives every `/api/chat` call a fresh graph thread ID.
The database history lookup happens in `bobclaw-gateway/routers/chat.py`, not in
the direct core endpoint used by this bridge. Reusing a core `conversation_id`
therefore does not replay conversational context by itself.

For correctness, the bridge flattens system/developer messages into a leading
`System:` section and user/assistant/tool messages into a labeled transcript.
The 30-message guard bounds replay cost. The bridge still derives a stable UUID
conversation ID for each CLI session:

1. Codex's `session-id` header;
2. body `session_id`, `conversation_id`, or `prompt_cache_key`;
3. the same keys inside `metadata` or `client_metadata`;
4. a deterministic hash of the first system/developer message and first user
   message.

This means every bob call remains a normal `/api/chat` turn and memory is never
disabled or bypassed. Because core does not replay direct-call history, the L0
`user_message` for a bridge turn contains the bounded flattened transcript.

## Install the user unit

Land the bridge in the production pin first. Then create
`~/.config/systemd/user/bobclaw-bridge.service` with:

```ini
[Unit]
Description=BoB OpenAI-compatible loopback bridge (:8902)
Wants=bobclaw-core.service
After=bobclaw-core.service

[Service]
Type=simple
WorkingDirectory=%h/Projects/bob/bobclaw-core
Environment=BOB_BRIDGE_MAX_HISTORY=30
# Optional. Without this, non-bob models fail closed with HTTP 501.
# Environment=BOB_BRIDGE_UPSTREAM=https://your-openai-compatible-host/v1
ExecStart=%h/Projects/bob/.venv/bin/python tools/bob_bridge.py --port 8902
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Review the unit file, then arm and inspect it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now bobclaw-bridge.service
systemctl --user status bobclaw-bridge.service --no-pager
journalctl --user -u bobclaw-bridge.service -n 50 --no-pager
```

The service must use the same populated venv as core; no dependency install is
needed because `aiohttp`, Pydantic, and PyYAML already ship with bobclaw-core.

## Health checks

```bash
curl --fail --silent http://127.0.0.1:8902/health | jq
curl --fail --silent http://127.0.0.1:8902/v1/models | jq -r '.data[].id'
ss -ltn '( sport = :8902 )'
```

Expected: health reports `status: ok`; every model ID starts with `bob`; and the
listener is only `127.0.0.1:8902`.

Check the fail-closed paths before client rollout:

```bash
curl --silent --output /tmp/bob-bridge-unknown.json --write-out '%{http_code}\n' \
  http://127.0.0.1:8902/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"bob-no-such-face","messages":[{"role":"user","content":"test"}]}'
jq . /tmp/bob-bridge-unknown.json

curl --silent --output /tmp/bob-bridge-planner.json --write-out '%{http_code}\n' \
  http://127.0.0.1:8902/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"bob-planner-codex","messages":[{"role":"user","content":"test"}]}'
jq . /tmp/bob-bridge-planner.json
```

Both must return `503`, and neither may reach `/api/chat`.

## Codex

Current Codex supports only the Responses wire protocol; `wire_api = "chat"` is
rejected. This is confirmed both by the checked `codex-rs` source and the
[official OpenAI configuration reference](https://developers.openai.com/codex/config-reference),
which lists `responses` as the sole supported value. That is why the bridge also
serves `/v1/responses` even though Hermes and Kimi use Chat Completions.

Add the provider to `~/.codex/config.toml`:

```toml
[model_providers.bob]
name = "BoB Bridge"
base_url = "http://127.0.0.1:8902/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
```

Current Codex profile files live beside `config.toml`. Create
`~/.codex/bob.config.toml`:

```toml
model_provider = "bob"
model = "bob"
```

Use the profile so selecting `-m bob` cannot accidentally leave the provider set
to OpenAI:

```bash
codex --profile bob
codex --profile bob -m bob-assistant
```

For a one-off without the profile file:

```bash
codex -c 'model_provider="bob"' -m bob
```

Codex sends its stable session identity in the `session-id` header. The bridge
maps it to a stable UUID and logs only its short hash.

## Kimi Code

Do not use `kimi provider add` for this loopback bridge. That command imports a
models.dev-shaped `api.json` registry over HTTP and requires a registry bearer
key; `/v1/models` by itself is not such a registry. The bridge deliberately does
not add a second registry surface.

For the current Kimi Code CLI, edit `~/.kimi-code/config.toml` (older Kimi CLI
installs used `~/.kimi/config.toml`) and merge these blocks:

```toml
[providers.bob]
type = "openai"
base_url = "http://127.0.0.1:8902/v1"
api_key = "local-loopback-no-auth"

[models.bob]
provider = "bob"
model = "bob"
max_context_size = 120000
display_name = "BoB Bridge"

[models.bob-assistant]
provider = "bob"
model = "bob-assistant"
max_context_size = 120000
display_name = "BoB Assistant"
```

The placeholder API key only satisfies Kimi's provider schema; the loopback
bridge ignores authorization. Validate the candidate file before replacing the
live one:

```bash
kimi doctor config /path/to/candidate-config.toml
kimi -m bob -p 'Say hello in one sentence.'
```

Kimi's official [provider configuration](https://moonshotai.github.io/kimi-code/en/configuration/config-files)
documents `type = "openai"`, `base_url`, and the provider/model tables.

## Hermes

Merge this block into `providers:` in `~/.hermes/config.yaml`; it matches the
existing `zai`/local provider shape:

```yaml
providers:
  bob:
    name: BoB Bridge
    base_url: http://127.0.0.1:8902/v1
    api_mode: chat_completions
    default_model: bob
    models:
      - bob
      - bob-assistant
    context_length: 120000
    discover_models: true
```

No `key_env` is required for the unauthenticated loopback endpoint. Validate the
candidate file before replacing the live one:

```bash
hermes config check
hermes --provider bob -m bob --pass-session-id -z 'Say hello in one sentence.'
```

`--pass-session-id` puts Hermes's stable session ID in its system prompt, making
the bridge's system-plus-first-user fallback identity unique per Hermes session.

## Live memory smoke

This calls a real configured bob backend and can incur its normal small model
cost. Record counts before the turn, use a unique harmless marker, and do not put
secrets in it.

```bash
cd <your-bob-checkout>/bobclaw-core
memory_db=.memory/bobclaw_memory.db
sqlite3 "$memory_db" 'select count(*) from memory_events;'
sqlite3 "$memory_db" "select count(*) from memory_writer_completions where status='COMPLETED';"
curl --fail --silent http://127.0.0.1:56333/collections/bobclaw__2560 | jq '.result.points_count'

smoke_session="$(uuidgen)"
curl --fail --silent http://127.0.0.1:8902/v1/chat/completions \
  -H 'content-type: application/json' \
  -d "{\"model\":\"bob\",\"stream\":false,\"metadata\":{\"session_id\":\"$smoke_session\"},\"messages\":[{\"role\":\"user\",\"content\":\"BOB-BRIDGE-SMOKE-20260901: reply with acknowledged.\"}]}" | jq .
```

Wait for the background projection to drain, then verify without printing the
stored message body:

```bash
event_id="$(sqlite3 "$memory_db" "select event_id from memory_events order by insertion_order desc limit 1;")"
sqlite3 "$memory_db" "select event_id, kind, length(json_extract(body_json,'$.user_message')) > 0 from memory_events where event_id='$event_id';"
sqlite3 "$memory_db" "select source_event_id,status,chunk_count from memory_writer_completions where source_event_id='$event_id';"
curl --fail --silent http://127.0.0.1:56333/collections/bobclaw__2560 | jq '.result.points_count'
journalctl --user -u bobclaw-core.service --since '-5 minutes' --no-pager | rg "t0_projected event_id=$event_id"
```

The L0 row must have a populated user message, the ledger row must be
`COMPLETED`, the T0 log must name the same event, and the Qdrant point count must
increase when the response is not a duplicate.

For cross-conversation recall, state a unique harmless fact in session A, then
ask for it from a fresh session B after T0 completes. Use two different UUIDs in
`metadata.session_id`. Confirm the answer and the two resulting L0/T0 rows. Leave
the clearly labeled smoke events in the append-only audit log; do not delete or
rewrite L0.

## Rollback

Client rollback is independent of memory data:

- Codex: stop using `--profile bob`, then remove `bob.config.toml` and the
  `[model_providers.bob]` block if desired.
- Kimi: restore the prior `default_model` and remove the two model blocks plus
  `[providers.bob]`.
- Hermes: select the previous provider and remove `providers.bob`.
- Service: `systemctl --user disable --now bobclaw-bridge.service`.

Stopping the bridge does not remove any L0 event, T0 vector, or writer-ledger
row already created by normal bob turns.

## Deferred options

- A memory-graph view joining bridge session hashes to L0 events, T0 projections,
  and completion-ledger rows.
- Evidence-driven automatic face selection by task type.
- A Claude lane only if Claude is later promoted from the back seat.
