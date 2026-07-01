# AGENTS-SETUP — install BoB (agent-runnable)

This is the agent-runnable twin of `install-bob.ps1`. If you are an AI agent (or a
human who prefers to run the steps by hand), follow these in order. They are
**Windows-only** for v0.95 and idempotent — safe to re-run.

The fastest path is simply to run the installer:

```powershell
./install-bob.ps1
```

If you run the steps manually instead, do them in this order.

## 0. Prerequisites (fail-closed)

- **`uv`** (Python manager): https://docs.astral.sh/uv/ — verify with `uv --version`.
- **PowerShell 7 (`pwsh`)**: `winget install --id Microsoft.PowerShell`. Windows ships
  only PowerShell 5.1, but every `scripts/win/*` service script requires `pwsh`.
- **Docker Desktop**, running: verify with `docker info`. Docker is **required** — it
  hosts Postgres/Redis/Qdrant, **and** it is the isolation boundary for the build/verify
  sandbox, which runs LLM-written code. If Docker is absent, stop and install it; do
  **not** work around the sandbox (`BUILD_SANDBOX=subprocess` runs generated code on the
  host — only for fully trusted models).

## 1. Python environment + pinned dependencies

```powershell
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -r bobclaw-core\requirements.lock
uv pip install --python .venv\Scripts\python.exe -r bobclaw-gateway\requirements.lock
uv pip install --python .venv\Scripts\python.exe -r bobclaw-claude-pipeline\requirements.lock
```

Use the `requirements.lock` files (fully pinned, `aiohttp<3.14`), not `requirements.txt`.

## 2. Env file + database password (BEFORE the first `compose up`)

1. `Copy-Item .secrets\bobclaw.env.example .secrets\bobclaw.env` (if it doesn't exist).
2. Generate a strong `POSTGRES_PASSWORD`, write it into `.secrets\bobclaw.env`, and update
   `POSTGRES_URL` to match (`postgresql://bobclaw:<password>@localhost:5432/bobclaw`).

Do this **before** step 3: the DB volume bakes in whatever `POSTGRES_PASSWORD` is set on
its first `up`, so setting the strong password first (and passing `--env-file` below) makes
the container and the app agree on the first try — no `docker compose down -v` dance.

## 3. Docker infrastructure + DB init

```powershell
docker compose --env-file .secrets\bobclaw.env up -d postgres redis qdrant
# wait until: docker exec bobclaw-postgres pg_isready -U bobclaw  → "accepting connections"
```

`--env-file` makes compose interpolate the SAME `POSTGRES_PASSWORD` the app uses (it is read
from `.secrets`, not the shell env). `init.sql` runs automatically on first volume init. All
infra ports bind to `127.0.0.1` only (see `SECURITY.md`).

## 4. Auth secrets + a backend

1. `.venv\Scripts\python.exe scripts\gen_secrets.py` — fills `BOBCLAW_SECRET`,
   `BOBCLAW_PASSWORD_HASH` (prints the plaintext admin password once), and `TOTP_SECRET`.
   `BOBCLAW_SECRET` **must be identical** for core and gateway (this one file is read by
   both) — the scope vouch depends on it.
2. Enable at least one backend:
   - **Paste** an `ANTHROPIC_API_KEY=sk-ant-...` into `.secrets\bobclaw.env`; or
   - **Detect-and-instruct**: if the `claude` CLI is installed, run `claude setup-token`
     (headless subscription auth — no API key; see `COMPLIANCE.md`); or
   - **Local only**: run Ollama or LM Studio and set `PREFERRED_LOCAL_MODEL`.

## 5. Durability (optional)

```powershell
./scripts/win/install-durability.ps1 -IncludeModels:$false
```

Registers Task-Scheduler tasks so core/gateway auto-start on logon after a reboot. Skip it
if you don't want auto-start, or if your environment blocks scheduled-task creation — it is
optional and does not affect the running stack.

## 6. Start + health-wait

```powershell
./scripts/win/start-local.ps1
# poll until healthy: Invoke-RestMethod http://127.0.0.1:7826/health
```

`start-local.ps1` brings up infra + core + gateway (+ the LiteLLM proxy if
`litellm/config.yaml` is present) as plain windows — **no local embedding models and no
Task-Scheduler dependency**. (`start-all.ps1` is the durable Task-Scheduler variant.)

## 7. Smoke test + first login

If an Anthropic key is set, confirm the default model resolves with a 16-token
`POST https://api.anthropic.com/v1/messages` using `model = ANTHROPIC_MODEL` (default
`claude-sonnet-5`). A non-error response means the key + model default are valid.
Local-only setups validate at first chat.

- Web UI: **http://127.0.0.1:7826/ui**
- Log in as **admin** with the password `gen_secrets` printed once.
- Login also requires a **TOTP 2FA code** — enroll `TOTP_SECRET` (from `.secrets\bobclaw.env`)
  in an authenticator app: `otpauth://totp/BoB:admin?secret=<TOTP_SECRET>&issuer=BoB`.
- Stop with `./scripts/win/stop-all.ps1`.

## Notes / known first-run gotchas

- **Memory is OFF by default** and the chat path does not need the embedder/extractor
  (recall fail-opens). They are optional local model servers (`start-embedder.ps1` /
  `start-extractor.ps1`); set `BOBCLAW_EMBED_GGUF` / `BOBCLAW_EXTRACTOR_GGUF` to enable
  them. Unset, they warn and skip — they never block the stack.
- **Adding a backend key after core started** requires a **core restart** (env is read at
  startup): `./scripts/win/stop-all.ps1` then `./scripts/win/start-local.ps1`.
- **Codex — GPT via ChatGPT login** (`planner-gpt`, native, no proxy): run
  `codex login` (ChatGPT OAuth), then create `~/.codex/gpt.config.toml`:

  ```toml
  model = "gpt-5.5"           # set to a model your ChatGPT plan serves
  model_provider = "openai"   # built-in provider — uses your codex login
  model_reasoning_effort = "high"
  ```

  Then `codex exec -p gpt` (and the `planner-gpt` face) run GPT under your subscription.
- **Codex — non-OpenAI providers** (`planner-codex`; GLM/DeepSeek/Qwen) route through a
  local **LiteLLM proxy** — start it with `./scripts/win/start-litellm.ps1` (sample
  `litellm/config.yaml`). Codex 0.142+ wants a per-file `~/.codex/<profile>.config.toml`
  with `wire_api = "responses"`. See `COMPLIANCE.md`.
- To drive BoB headlessly from another agent, see the MCP server
  (`scripts/win/start-mcp.ps1`) and the `ARCHITECTURE.md` "operating model" section.
