<p align="center">
  <img src="bobclaw-app/brand/mascot-crest.png" alt="BoB" width="200">
</p>

<h1 align="center">BoB</h1>
<p align="center"><strong>Build · Orchestrate · Bind</strong> — a <a href="https://canopyseeds.com">Canopy Seed</a> project.</p>
<p align="center"><em>Own your AI. BoB does the rest.</em></p>

---

BoB is a self-hosted, multi-agent orchestration platform. It runs on **your**
machine, with **your** keys, and its job is everything *past* aggregating models:
routing work to the right model, fanning it out across many, deliberating in a
council, and — the part most tools skip — **verifying** the result before it reaches
you.

> **v0.96 — headless-first.** The CLI / MCP / agent front door is usable today. The
> web and desktop GUIs ship as a **preview**. Hardening and GUI polish are tracked to
> v1.0. This is an honest early release: read `SECURITY.md` before exposing the gateway.

## Why BoB

The most capable models live behind someone else's login, and that access can change
under you — new gates, price changes, regional restrictions, waitlists. Renting an
intelligence is not the same as owning your workflow. **BoB is the opposite bet:**
your machine, your credentials, your data. Nobody can revoke it, because there is no
one in the middle.

BoB's value is the four things that come *after* "just call an LLM":

- **Verification.** BoB doesn't trust output — it checks it. Generated code runs in a
  locked-down, network-denied Docker sandbox before it's accepted; a council can
  ground its answer against the live web before converging; fan-out workers are
  gated by a critic before their results are joined.
- **Orchestration.** One request can route to a single fast face, fan out to many
  workers, escalate through a health-aware backend chain, or convene a multi-voice
  council — with per-turn cost and width caps so a run can't stampede.
- **Sovereignty.** Every backend is reached with your own key or the vendor's own
  official CLI under your own login. BoB never proxies, resells, or multi-tenants
  anyone's access. (See `COMPLIANCE.md`.)
- **Completeness.** Faces, teams, profiles, memory, a build pipeline, a council, a
  headless MCP server — the layer above aggregation, in one place you control.

## Quickstart (Windows, headless-first)

**Prerequisites** (all three are required; the installer fails closed without them):

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) — **running**
  (`docker info` must succeed). Hosts Postgres/Redis/Qdrant **and** the build/verify sandbox.
- [`uv`](https://docs.astral.sh/uv/) — the Python environment manager (`uv --version`).
- [**PowerShell 7+**](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)
  (`pwsh`). Windows ships only PowerShell 5.1; the durability and service scripts need `pwsh`.
  Quick install: `winget install Microsoft.PowerShell`.

```powershell
git clone <your-fork-url> bob
cd bob
./install-bob.ps1
```

The installer checks prerequisites, creates the Python environment from the pinned
lockfiles, brings up the Docker infrastructure, bootstraps your secrets
(interactively), waits for health, smoke-tests your default model, and prints the URL
and login. It is idempotent — safe to re-run.

Prefer to run the steps yourself, or drive setup from an agent? See
**[`AGENTS-SETUP.md`](AGENTS-SETUP.md)** — the same flow, step by step (it also lists the
known first-run gotchas on a fresh Windows box).

When it finishes:

- **Web UI (preview):** http://127.0.0.1:7826/ui — log in as `admin` with the
  generated `BOBCLAW_PASSWORD` printed once during setup (only its bcrypt hash is stored
  in `.secrets/bobclaw.env`). Login also requires a **TOTP 2FA code** — enroll the
  `TOTP_SECRET` (same file) in an authenticator app:
  `otpauth://totp/BoB:admin?secret=<TOTP_SECRET>&issuer=BoB`.
- **Stop:** `./scripts/win/stop-all.ps1`

### Enabling a backend

BoB needs at least one model backend. Each is enabled in `.secrets/bobclaw.env`, and
**model IDs must match what your provider currently serves** — the example values are
just placeholders. Core reads env at startup, so **restart core** after adding a key.

| Backend (face) | What to set | Auth |
| --- | --- | --- |
| **Cloud API** (Anthropic / Google / DeepSeek / Z.AI / Kimi / MiniMax) | the provider's `*_API_KEY` + `*_MODEL` | paste your key |
| **Claude CLI** (`planner-claude`) | `CC_CLI_PATH` (blank = resolve on PATH) | `claude setup-token` |
| **Antigravity** (`planner-gemini`) | `AGY_CLI_PATH` | run `agy` once → Google login |
| **Codex** (`planner-codex` / `planner-gpt`) | `CODEX_CLI_PATH`, `CODEX_HOME`; a LiteLLM proxy for non-OpenAI providers (`./scripts/win/start-litellm.ps1`) | ChatGPT login (GPT, native) / per-provider LiteLLM keys |
| **Local** (Ollama / LM Studio) | `PREFERRED_LOCAL_MODEL` + the server URL | none |

> **Codex note:** `codex exec` routes non-OpenAI providers (GLM/DeepSeek/Qwen) through a
> local **LiteLLM proxy** (`LITELLM_BASE_URL`, default `:4000`). Start it with
> `./scripts/win/start-litellm.ps1` (sample: `litellm/config.yaml`); Codex 0.142+ needs a
> per-file `~/.codex/<profile>.config.toml` with `wire_api = "responses"`. **GPT** under a
> ChatGPT login runs **natively** (no proxy) via the `planner-gpt` face.

### Bringing it back up

`stop-all` / a reboot stops the host services (Docker restarts itself). To relaunch,
re-run `./install-bob.ps1` (idempotent) or the lighter `./scripts/win/start-local.ps1`,
which brings up infra + core + gateway without requiring local embedding models.

## What's inside

Four services (see **[`ARCHITECTURE.md`](ARCHITECTURE.md)** for the full picture):

| Service | Port | Role |
| --- | --- | --- |
| `bobclaw-core` | 7825 | LangGraph engine — routing, faces, fan-out, council, memory, build pipeline, all model backends |
| `bobclaw-gateway` | 7826 | Auth (JWT + TOTP), chat, REST API, serves the web UI at `/ui` |
| `bobclaw-claude-pipeline` | 7823 | Claude build-session wrapper |
| `bobclaw-app` | — | Kotlin Multiplatform native client (desktop + Android) — **preview** |

Backends span cloud APIs (Anthropic, Google, DeepSeek, Z.AI/GLM, Moonshot/Kimi,
MiniMax), subscription CLIs run under your own login (`claude`, `codex`, `agy`,
`kimi`), and fully-local model servers (Ollama, LM Studio, llama.cpp). All model IDs
are configurable — set your provider's current model in `.secrets/bobclaw.env`.

## Documentation

- **[`AGENTS-SETUP.md`](AGENTS-SETUP.md)** — step-by-step / agent-runnable install
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — services, ports, and the operating model (when to council vs. single-dispatch, teams, capability classes)
- **[`SECURITY.md`](SECURITY.md)** — the loopback-by-default posture; read before exposing anything
- **[`COMPLIANCE.md`](COMPLIANCE.md)** — using your own subscriptions/keys within each vendor's terms
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — running the tests, project layout
- **[`CHANGELOG.md`](CHANGELOG.md)** — release notes

## Status & scope

v0.96 is headless-usable, GUI-preview, and single-operator. It is **loopback by
default**; the gateway can be exposed for trusted remote access **behind a
TLS-terminating reverse proxy** (see `SECURITY.md`) — `core` and the datastores stay
loopback. It is not a hardened multi-tenant public service. Containerized topology,
one-click packaging, and cross-platform support are on the roadmap to v1.0+.

## License

[Apache-2.0](LICENSE). A [Canopy Seed](https://canopyseeds.com) project.
