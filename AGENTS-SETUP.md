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
- **Docker Desktop**, running: verify with `docker info`. Docker is **required** —
  it hosts Postgres/Redis/Qdrant, **and** it is the isolation boundary for the
  build/verify sandbox, which runs LLM-written code. If Docker is absent, stop and
  install it; do **not** work around the sandbox (`BUILD_SANDBOX=subprocess` runs
  generated code on the host — only for fully trusted models).

## 1. Python environment + pinned dependencies

```powershell
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -r bobclaw-core\requirements.lock
uv pip install --python .venv\Scripts\python.exe -r bobclaw-gateway\requirements.lock
uv pip install --python .venv\Scripts\python.exe -r bobclaw-claude-pipeline\requirements.lock
```

Use the `requirements.lock` files (fully pinned, `aiohttp<3.14`), not
`requirements.txt`.

## 2. Docker infrastructure + DB init

```powershell
docker compose up -d postgres redis qdrant
# wait until: docker exec bobclaw-postgres pg_isready -U bobclaw   → "accepting connections"
```

`init.sql` runs automatically the first time the Postgres volume initializes. All
infra ports bind to `127.0.0.1` only (see `SECURITY.md`).

## 3. Secrets bootstrap

1. `Copy-Item .secrets\bobclaw.env.example .secrets\bobclaw.env` (if it doesn't exist).
2. Generate a strong `POSTGRES_PASSWORD`, write it into `.secrets\bobclaw.env`, and
   update `POSTGRES_URL` to match (`postgresql://bobclaw:<password>@localhost:5432/bobclaw`).
   If the Postgres volume already existed with the old password, run
   `docker compose down -v` once to re-initialize it.
3. `.venv\Scripts\python.exe scripts\gen_secrets.py` — fills `BOBCLAW_SECRET`,
   `BOBCLAW_PASSWORD`, `TOTP_SECRET`. `BOBCLAW_SECRET` **must be identical** for core
   and gateway (this single file is read by both) — the scope vouch depends on it.
4. Enable at least one backend:
   - **Paste** an `ANTHROPIC_API_KEY=sk-ant-...` into `.secrets\bobclaw.env`; or
   - **Detect-and-instruct**: if the `claude` CLI is installed, tell the operator to
     run `claude setup-token` (headless subscription auth — no API key; see
     `COMPLIANCE.md`); or
   - **Local only**: run Ollama or LM Studio and set `PREFERRED_LOCAL_MODEL`.

## 4. Durability (optional)

```powershell
./scripts/win/install-durability.ps1
```

Registers Task-Scheduler tasks so core/gateway/embedder/extractor survive a reboot
and auto-start on logon. Skip if you don't want auto-start.

## 5. Start + health-wait

```powershell
./scripts/win/start-all.ps1
# poll until healthy: Invoke-RestMethod http://127.0.0.1:7826/health
```

## 6. Smoke test

If an Anthropic key is set, confirm the default model resolves (this is the
"no model-not-found" check) with a 16-token `POST https://api.anthropic.com/v1/messages`
using `model = ANTHROPIC_MODEL` (default `claude-sonnet-5`). A non-error response
means the key and model default are valid. Local-only setups validate at first chat.

## 7. Use it

- Web UI: **http://127.0.0.1:7826/ui**
- Log in as **admin** with the `BOBCLAW_PASSWORD` value from `.secrets\bobclaw.env`.
- Stop with `./scripts/win/stop-all.ps1`.

## Notes

- The chat path does not require the embedder/extractor (memory recall fail-opens);
  those are optional local model servers (`start-embedder.ps1` / `start-extractor.ps1`)
  configured via env — see their headers and `.env.example`.
- To drive BoB headlessly from another agent, see the MCP server
  (`scripts/win/start-mcp.ps1`) and the `ARCHITECTURE.md` "operating model" section.
