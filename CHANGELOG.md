# Changelog

All notable changes to BoB are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [0.95.0] — unreleased (initial open-source candidate)

First public release candidate. **Headless-first** (CLI / MCP / agent is the usable
front door); the web and Kotlin Multiplatform desktop GUIs ship as a **preview**.
Designed for **loopback, single-operator** use — see `SECURITY.md` before exposing
anything to a network.

### Added
- Multi-agent orchestration engine (`bobclaw-core`, LangGraph): face routing,
  health-aware backend escalation, `Send`-based fan-out with per-backend cost/width
  caps, a deliberating council (fusion / sequential / debate shapes with a pre-close
  grounding gate), an in-graph build → verify → repair pipeline, an optional memory
  module, and a headless MCP server.
- Four services: `bobclaw-core` (7825), `bobclaw-gateway` (7826, auth + REST + web UI
  at `/ui`), `bobclaw-claude-pipeline` (7823), and `bobclaw-app` (Kotlin Multiplatform
  desktop/Android client — preview).
- Model backends spanning cloud APIs (Anthropic, Google, DeepSeek, Z.AI/GLM,
  Moonshot/Kimi, MiniMax), subscription CLIs run under your own login (`claude`,
  `codex`, `agy`, `kimi`), and local model servers (Ollama, LM Studio, llama.cpp).
- Guided Windows installer (`install-bob.ps1`) and an agent-runnable `AGENTS-SETUP.md`.
- Docker Compose infrastructure (Postgres / Redis / Qdrant), pinned dependency
  lockfiles for all three Python services, and `docker/build-sandbox.Dockerfile`.
- Apache-2.0 `LICENSE` + `NOTICE`; `README`, `ARCHITECTURE`, `SECURITY`, `COMPLIANCE`,
  and `CONTRIBUTING` documentation.

### Security
Baseline posture:
- **Loopback by default** — every service and all infrastructure bind `127.0.0.1`;
  core has no auth of its own and is never client-facing (it trusts a gateway HMAC
  scope vouch).
- **Build/verify sandbox** — LLM-written code runs inside a Docker container with
  `--network none`, a read-only root filesystem, dropped capabilities, and no host
  secrets mounted. `BUILD_SANDBOX` defaults to `docker` (fail-closed).
- **Auth** — JWT access tokens, TOTP with RFC 6238 replay protection, and scoped,
  default-deny agent bearer tokens.
- Secrets live only in the git-ignored `.secrets/bobclaw.env`; BoB never bundles,
  proxies, or transmits your provider credentials anywhere but the provider itself
  (`COMPLIANCE.md`).

Hardening applied after a security review of the initial candidate:
- `/health` no longer exposes internal service URLs (the service map moved behind auth).
- Security-response-headers middleware on every response: `Content-Security-Policy`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`.
- Per-IP failed-login lockout on `/auth/login` with exponential backoff (persisted).
- Refresh tokens gain a shorter sliding TTL (30d) **and** an absolute rotation-chain
  cap, so a stolen token cannot be rotated indefinitely; `POST /auth/revoke-all` kills
  every session at once.
- Admin password stored as a **bcrypt hash** at rest (`BOBCLAW_PASSWORD_HASH`); the
  plaintext is generated and shown once, never written to disk.
- `ALLOWED_ORIGINS` rejects wildcard/malformed entries at startup.
- `gen_secrets` now regenerates the `BOBCLAW_SECRET` example placeholder (it was kept
  verbatim before, so a default install could ship a publicly-known JWT / scope-vouch key).
- A "before you expose to a network" checklist added to `SECURITY.md`.

### Fixed
From a guided first install on a clean Windows box:
- **Core boots as shipped** — `start.py` read `config.BUILD_SANDBOX` off the config
  instance, but it (and other trailing settings) are module globals, so the server died on
  startup with an `AttributeError` while the unit suite stayed green. Now read via the
  module, with a boot smoke test guarding the regression.
- **The stack starts with no local models** — `start-embedder.ps1` / `start-extractor.ps1`
  now warn-and-skip (instead of aborting the whole stack) when their GGUF env vars are
  unset, and a new `scripts/win/start-local.ps1` brings up infra + core + gateway with no
  local-model or Task-Scheduler dependency. The installer uses it.
- **First-run database password** — the installer generates the strong `POSTGRES_PASSWORD`
  before the first `compose up` and passes `--env-file`, so the DB volume and the app agree
  on the first try (no `docker compose down -v`).
- **PowerShell 7 (`pwsh`)** is now a fail-closed prerequisite (the service scripts require
  it; Windows ships only 5.1), and the installer surfaces the TOTP enrollment URI that a
  first login needs.

### Added (Codex / GPT, out of the box)
- `planner-gpt` face — GPT via a ChatGPT-subscription login through the `codex` CLI
  (native, no proxy). A sample `litellm/config.yaml` + `scripts/win/start-litellm.ps1` stand
  up the LiteLLM proxy that Codex's non-OpenAI providers (GLM / DeepSeek / Qwen) route
  through. (`planner-gpt` was live-verified against a ChatGPT login through BoB's own
  `codex_code` path; the LiteLLM/DeepSeek proxy route is not covered by automated tests —
  validate it against your provider.)

### Notes
- v0.95 is a single-operator, loopback release and is **not** intended to be exposed to
  the internet as-is. Containerized topology, one-click packaging, and cross-platform
  support are tracked toward v1.0+.
