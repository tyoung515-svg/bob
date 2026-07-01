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

> **v0.95 — headless-first.** The CLI / MCP / agent front door is usable today. The
> web and desktop GUIs ship as a **preview**. Hardening and GUI polish are tracked to
> v1.0. This is an honest early release: read `SECURITY.md` before exposing anything.

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

Prerequisites: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(running) and [`uv`](https://docs.astral.sh/uv/).

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
**[`AGENTS-SETUP.md`](AGENTS-SETUP.md)** — the same flow, step by step.

When it finishes:

- **Web UI (preview):** http://127.0.0.1:7826/ui — log in as `admin` with the
  generated `BOBCLAW_PASSWORD` (in `.secrets/bobclaw.env`).
- **Stop:** `./scripts/win/stop-all.ps1`

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

v0.95 is headless-usable, GUI-preview, and single-operator / loopback by design.
It is **not** meant to be exposed to the internet as-is. Containerized topology,
one-click packaging, and cross-platform support are on the roadmap to v1.0+.

## License

[Apache-2.0](LICENSE). A [Canopy Seed](https://canopyseeds.com) project.
