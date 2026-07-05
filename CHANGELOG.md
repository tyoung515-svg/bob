# Changelog

All notable changes to BoB are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [0.97.0]

### Added
- **Chinese localization (Simplified + Traditional).** The desktop app's UI is localized
  — 172 UI strings in en / zh-Hans (简) / zh-Hant (繁), a restart-free header language
  toggle (EN → 简 → 繁), and a role-label map. The backend threads an optional
  per-turn `locale`: when it is non-`en`, the model is directed to reply in that language
  (the desktop app sends the locale per-turn; a `switch_locale` WS message can also pin it
  to a conversation). Absent / `en` ⇒ byte-identical to before.
- **Codex planner honors an explicitly-pinned model.** When a specific model is pinned on the
  `gpt` / codex planner tier (via `switch_model` / `state.model_override`), a `gpt`-profile
  face now runs that chosen GPT model (e.g. `gpt-5.5`) natively under a ChatGPT login instead
  of only the profile's default — without being forced through the LiteLLM proxy. (The desktop
  app pins the backend/face today; a model *picker* control in the GUI is a follow-up.)
- **Faces know they're running inside BoB.** A spawn-identity card prepends a system line to
  every turn naming the platform, the face (name / role), and the backend serving it — so a
  face answers "I'm BoB's General Assistant, served by …" instead of "I have no idea I'm
  deployed." The code default is **off** (`BOB_IDENTITY_ENABLED`, byte-identical); the shipped
  `.env` deliberately **opts in** (set it false for a bare model). `BOB_IDENTITY_TEXT` overrides.

### Changed
- **Removed the preview web UI — the desktop app is the GUI.** The Preact browser stopgap
  (`bobclaw-gateway/ui/*`) is retired; the gateway now serves the JSON + WebSocket API only
  (`/` returns a small info response instead of redirecting to `/ui`). The Kotlin
  Multiplatform desktop app is the client (Android preview). This also removes the browser
  `localStorage` session-token surface. Docs updated throughout.

### Fixed
- **Codex `health_check` no longer strands a native-GPT face on a down proxy.** It gated on
  the LiteLLM proxy unconditionally, so under a team / JOAT health-walk a `planner-gpt` face
  (which needs no proxy) was wrongly marked unhealthy whenever `:4000` was down. Health is
  now the codex-CLI liveness only; a litellm-routed profile that hits a down proxy escalates
  at runtime via the existing chain.

### Notes
- Compose Multiplatform bumped 1.6.11 → 1.7.3 (KMM app).
- The KMM localization passes `:shared:jvmTest`; the runtime language-toggle *visual* check
  is a manual step (needs a display). Native-speaker review of the translations and the
  LiteLLM-via-Codex worker path remain user-validated.
- The language directive is applied on the apex/synthesis reply; fan-out worker sub-turns
  don't yet inherit the conversation locale (a follow-up — the surfaced answer is localized).
- A few interpolated / parameterized status strings in the app still render English (a
  `%s`-formatting follow-up); the 172 static UI strings are fully translated.

## [0.96.0] — first public release

**Headless-first** (CLI / MCP / agent is the usable front door); the web and Kotlin
Multiplatform desktop GUIs ship as a **preview**. Single-operator, **loopback by
default** — the gateway is exposable for trusted remote access behind a
TLS-terminating reverse proxy (see `SECURITY.md`). Supersedes the internal 0.95
review candidate (never published).

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
  lockfiles for all three Python services, and `bobclaw-core/docker/build-sandbox.Dockerfile`.
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
- v0.96 is single-operator and loopback by default; the gateway is exposable for trusted
  remote access behind a TLS-terminating reverse proxy (see `SECURITY.md`), but it is not
  a hardened multi-tenant public service. Containerized topology, one-click packaging, and
  cross-platform support are tracked toward v1.0+.
