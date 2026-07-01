# Architecture

BoB is four cooperating services plus a native client. This document describes how
they fit together and — in the **Operating model** section at the end — how to think
about *when* BoB should route to a single model, fan out to many, or convene a
council.

## Services

| Service | Tech | Port | Role |
| --- | --- | --- | --- |
| `bobclaw-core` | Python / LangGraph | 7825 | The engine: routing, faces, fan-out dispatch, council, memory module, the build pipeline, and every model backend. **Has no auth of its own** — it trusts the gateway's HMAC scope vouch. Loopback-only. |
| `bobclaw-gateway` | Python / aiohttp | 7826 | JWT + TOTP auth, WebSocket chat, REST (conversations / messages / faces / models / teams / profiles / approvals / memory), and serves the web UI at `/ui`. The only client-facing service. |
| `bobclaw-claude-pipeline` | Python | 7823 | Claude build-session wrapper (holds an Anthropic key). |
| `bobclaw-app` | Kotlin Multiplatform | — | Native client (desktop + Android). **Preview** in v0.96. |

Infrastructure (Docker, all bound to `127.0.0.1`): Postgres `5432`, Redis `6379`,
Qdrant `6353/6354`, and an optional Playwright `3100`. Optional local model servers
run as host processes: embedder `8081`, extractor `8082`, on-demand research head
`8083`.

BoB runs **multi-process** in production — multiple `core` workers can serve dispatch
concurrently behind one gateway. In-memory process-local state in `core/` is
therefore treated as a correctness concern, not a single-tenant assumption; escalation
pins are Redis-backed.

## Request flow

A chat turn runs through a LangGraph state machine in `core`:

```
route ──► recall ──► dispatch ─┬─ (<threshold subtasks) ─► execute ─► END
                               │
                               └─ (≥threshold subtasks) ─► worker ×N ─► join ─┬─► dispatch (next wave)
                                                                              └─► verify / execute ─► END
```

- **route** selects a *face* (a persona + backend + posture) from the request. A
  planning-shaped task escalates to a planner face; a code-shaped task routes to a
  worker face.
- **recall** (optional, default off) splices relevant memory facts into the prompt;
  it **fail-opens** — a missing/dangling vector never blocks the turn.
- **dispatch** either runs a single **execute** node or, past a fan-out threshold,
  emits N **worker** nodes (Send-based), bounded by per-backend width and cost caps
  and a global ceiling. Results merge in **join**; a critic gates workers before the
  join.
- **council** and **build** are subgraphs the router can divert into (below).

### Escalation (health-aware)

Faces carry a `preferred_backend` and an ordered escalation chain. A live health
probe (per-backend reachability + rate-limit pins) lets routing **walk around** a
throttled or unhealthy backend to the next in the chain, fail-open. Backends are bare
strings (`claude_api`, `claude_code`, `deepseek_v4_flash`, `glm_5_2`, `minimax`,
`gemini_flash`, `kimi_code`, `agy_code`, `codex_code`, `opencode_serve`, `local`, …);
subscription-CLI backends run under your own login and carry no metered key.

## The build pipeline (verification, made concrete)

A chat turn carrying a build request runs `plan_contracts → dispatch → worker×N →
join → verify → {repair → verify}* → END` in-graph. The apex model plans contracts;
worker models implement them; the **verify** node is the sole emitter and runs the
generated code inside a **locked-down Docker container** (`BUILD_SANDBOX`): only the
per-turn workspace is mounted, `--network none`, resource caps, ephemeral. A gate that
can't pass **surfaces** the failure honestly — it never edits a test to force green. A
static analysis gate cannot contain Python; the container is the real boundary. See
`SECURITY.md`.

## The council

For non-trivial design/analysis, a council deliberates instead of a single answer:

- **fusion** — seats answer blind in parallel, then a synth seat reconciles.
- **sequential** — a framer → stress → synth chain in one pass.
- **debate** — seats loop round-robin, seeing prior-round positions, until the active
  idea set converges (no-delta) or a round/cost budget binds.

A **pre-close grounding gate** can verify the answer's load-bearing claims against the
live web before convergence and trigger a bounded grounded restart on drift. Seats map
to backends by *posture* (framer / stress / wildcard / synth), each with a fallback
chain — never hard-bound to one vendor.

## Memory (bring-your-own; default off)

The memory *module* ships; a corpus never does. L0 is a SQLite event log; L1 vectors
live in Qdrant. Recall is off by default (`MEMORY_ENABLED=false`) and fail-opens when
on. A federation registry lets BoB read multiple local corpora you register
(`data/ledger_instances.example.json` shows the shape) under a single-writer fence —
BoB writes only its own collection; your corpora stay read-only to it. A fresh install
boots and routes with empty memory.

## Headless / BYO-agent

BoB publishes itself as an **MCP stdio server** (`scripts/win/start-mcp.ps1`) exposing
`chat_with_face` and `run_council` as thin proxies to the gateway, authed by a scoped,
default-deny **agent bearer token**. An agent token reaches only conversation
endpoints; admin routes are 403; the token's scope rides to core under the HMAC vouch,
so an irreversible action still routes to a human gate.

---

## Operating model

BoB gives you several ways to spend compute on a request. Choosing well is most of
using it effectively.

### Single-dispatch vs. fan-out vs. council

- **Single dispatch** — one face, one backend. Use for the overwhelming majority of
  turns: a question, an edit, a lookup, a scoped implementation. Fast and cheap.
- **Fan-out** — many workers in parallel under one apex, joined and critic-gated. Use
  when the work *decomposes* into independent units (implement N contracts, review N
  files, check N candidates). Bounded by width/cost caps so it can't stampede.
- **Council** — multiple voices deliberate to one answer. Use for **decisions**, not
  labor: architecture choices, trade-off analysis, anything where independent
  perspectives and an adversarial pass beat a single confident answer. Councils cost
  more tokens; reserve them for turns where being *right* matters more than being fast.
- **Build pipeline** — when the deliverable is runnable code that should be *verified*,
  not just written. The Docker verify gate is the point.

Rule of thumb: **single-dispatch by default; fan-out when the task splits into
independent pieces; council when the cost of being wrong is high; build when the output
must run.**

### Teams and profiles

- A **team** maps roles (`apex` / `worker` / `critic`) to backends — e.g. a strong
  model plans and synthesizes while cheap parallel workers do the labor and a
  mid-tier model audits. The default team is a byte-for-byte passthrough (no
  regression); teams are opt-in per conversation.
- A **profile** is a team plus the *how* layer: per-slot role prompts, a deliberation
  shape (fusion / sequential / debate), protocol bounds (max rounds, grounding, and a
  cost ceiling on the metered paths), and an optional schedule. Profiles let you name and reuse a working
  shape.

### Capability classes, not model names

Route by *capability class*, not by a hard-coded vendor model. A seat/role is defined
by the job it does — "plan," "bulk worker," "auditor," "reconcile" — and mapped to a
backend plus an ordered fallback chain. When a preferred backend is missing or
throttled, routing falls through the chain rather than failing or silently pinning one
vendor. This keeps a fleet steerable: swap a class's backing model in one place, and
every role that uses that class follows. Set your own model IDs per backend in
`.secrets/bobclaw.env`.
