# Architecture

BoB 0.99.1 has a desktop app and a server runtime. A lane is a named space
with its own sign-in, service processes, conversations, memory files and workspace.
The packaged runtime provisions these parts together. A standalone server built
from source provisions a smaller set; see [AGENTS-SETUP.md](AGENTS-SETUP.md).

## Parts

| Part | Location | Responsibility |
| --- | --- | --- |
| Desktop app | `portal/shell` | React interface in a Tauri window, with native Rust transport, setup and PC controls. |
| App engine | `portal/engine` | Rust request, policy, approval, ledger and projection machinery; also supports the app's local engine surfaces. |
| Broker | `portal-broker/portal-broker.py` | Exchanges newline-delimited JSON over standard input/output and calls a loopback gateway using a server-held portal bearer. |
| Gateway | `bobclaw-gateway` | Password/authenticator login, token checks, conversations, REST and WebSocket APIs, and owner decisions. |
| Core | `bobclaw-core` | Agent routing, model backends, tools, memory, workflows and orchestration. |
| Bridge | `bobclaw-core/tools/bob_bridge.py` | Loopback model-protocol adapter to core, separate from the desktop broker. It has no login. |
| Workers | `bobclaw-core/core/worker` and `deploy/runtime` | Execute enrolled jobs under a separate lane worker identity with filesystem and network restrictions. |
| Datastores | Postgres, Redis, Qdrant and SQLite | Application records, messaging/state, vectors and lane-local durable records. |
| Runtime image | `deploy/runtime` | Ubuntu filesystem with systemd services, vendor CLIs, datastore setup, lane provisioning and worker controls. The Windows installer imports it into WSL 2. |
| Other front ends | `bobclaw-tui`, `bobclaw-telegram` | Terminal and Telegram clients of the gateway, for source installations. Telegram additionally requires a bot token and an explicit numeric user allowlist. |

`bobclaw-app` is the earlier Kotlin app; it does not work with this release and
is kept until the mobile app is rebuilt.

## Lanes and shared services

In the runtime image, each lane gets separate core, gateway and bridge ports,
Linux identities for its services and worker, login/signing secrets, a memory
SQLite database, a gateway SQLite database and workspace directories. Lane data
starts under `/var/lib/bob/family/<lane>/bob-memory`; gateway authority state is
provisioned separately under `/var/lib/bob-gateway/<lane-id>`.
Qdrant collection prefixes are selected per lane.

This separation does not create an independent server for each lane. Core and
gateway receive the same installation-wide Postgres URL and Redis URL. Application
queries filter records by owner and, on newer execution surfaces, by lane as well.
The database credentials do not confine a service to one lane's rows. Qdrant,
the embedder, installed code, the Linux kernel and runtime administration are also
shared. Lanes suit an installation controlled by one owner; they are not a wall
between mutually untrusted users or protection from the machine administrator.

The first available runtime lane normally uses core 7835, gateway 7836 and bridge
8902, all on IPv4 loopback. Later lanes receive unused ports. Postgres 5432, Redis
6379, Qdrant 6353/6354 and the optional embedder 58081 stay on loopback. Core and
the bridge are internal services: a core scope signature is not a login gate for
its whole API. Keep these ports private; see [SECURITY.md](SECURITY.md).

## One desktop turn

1. The desktop app starts the broker inside the local runtime, or starts a remote
   broker through SSH. Remote SSH uses batch authentication and strict host-key
   checking. The broker reads its private portal token on the server; the app
   does not need a copy of that bearer.
2. The broker authenticates to the lane gateway. The gateway checks the token and
   conversation ownership, stores the message and fetches prior messages. It
   forwards the turn to core, including the selected agent, backend/model,
   conversation context and any token scope under an HMAC signature.
3. Core routes the turn, recalls memory when enabled, fits context and calls the
   selected backend. An agent configuration is called a *face* in the code. API
   backends use enrolled keys; ChatGPT subscription turns use the stored Codex
   sign-in directly, while Claude subscription turns run Claude Code.
4. A tool request reaches its server policy before execution. For a Builder
   operation needing approval, core records a pending operation and streams an
   approval card. The turn waits for the owner's decision or expires. An allowed
   operation is dispatched to the lane worker, then its result returns to the
   model. Suggest mode can return a proposal without executing it. Auto and
   temporary YOLO permissions can avoid individual prompts within their bounds.
5. Output streams from core through the gateway and broker to the app. The
   gateway stores the response. Worker completion and approval records are
   separate from model prose: an answer alone does not prove a command succeeded.

Owner decisions use a separate short-lived owner session, not the model's chat
scope. The installed default allows ordinary work confirmations by the signed-in
app user; changing the approval policy still requires password and authenticator
proof. Windows PC access has a separate native consent/grant path in the desktop
app. A runtime Builder permission does not grant Windows access.

## Execution boundaries

A runtime worker runs as its lane's unprivileged worker user, without supplementary
groups or Linux capabilities. The systemd unit restricts mounts, devices, process
visibility and resources. Before launching job code, the worker installs Landlock rules
(requiring Linux Landlock ABI 3 or later) and syscall filters before launching
code. A Builder job can write its selected project workspace and private temporary
directory, and read permitted runtime files. Subscription profiles have explicit
access to their own lane's CLI sign-in home; this is not a claim that every worker
profile is credential-free.

Worker traffic is restricted by user-ID firewall rules to the lane egress proxy.
Jobs without networking also block connection syscalls. The proxy checks public
addresses and limits destination ports to HTTP 80 and HTTPS 443. Builder requests
use a built-in allowlist, project additions and owner decisions; non-allowlisted
requests normally pause for approval. YOLO or a matching prior grant broadens
that policy. HTTPS CONNECT authorizes a destination tunnel: the proxy cannot
inspect encrypted methods or content within it. This is not data-loss prevention.

## Installation shapes

| Shape | What is provisioned |
| --- | --- |
| Windows installer / runtime image | WSL 2 runtime, shared datastores/schema, per-lane services, broker credentials, bridge, isolated workers, egress proxy, subscription enrollment and runtime credential/recovery controls. The embedder is an optional download. |
| Standalone Ubuntu server from source | Core and gateway as host services, one `admin` lane, private environment and broker launcher/token. Datastores can be native services or the root Compose file. The terminal client and a desktop SSH connection use this gateway. |

The install script does not install image worker services, subscription enrollment,
credential/recovery controls or bridge-dependent PC features. Docker in that route
provides datastores and can support the older contract-build verification gate;
it does not substitute for the runtime worker used by the desktop Builder.
Do not expect every desktop control to be available on a standalone server.

## Orchestration, builds and memory

The core graph contains single-agent execution, bounded fan-out with a join,
hierarchical dispatch, and council paths. A council can combine independent
answers (fusion), run a sequence, or debate within configured bounds; grounding
can add web evidence when configured. These are core capabilities, not a promise
that every installed lane has enrolled models for every role. Configured-lane
ordinary chat and explicitly pinned backends must be distinguished from optional
roster-based routing and escalation.

The older contract build path is explicitly triggered with `build_request`:
plan contracts, dispatch implementations, join, verify, and repair within a budget.
Repair changes implementations, leaving contracts/tests intact. The desktop broker
has no ingress for this flag. In the image, verification uses workers; on a
standalone host, explicit `BUILD_SANDBOX=docker` requires the sandbox image and
fails if unavailable. Its container mounts only the generated workspace read-only,
has no network, and uses a read-only root plus resource limits. Standalone `auto`
can fall back to host execution; it is not an isolation guarantee.

Memory stores events in SQLite and retrieval vectors in Qdrant. The runtime
lane enables verbatim projection with a completion ledger and recall, using a
lane collection prefix and a write fence. Fact extraction is separate and disabled
in new runtime lanes; its slots are deferred. Source setup disables memory until
configured. Optional federation reads require explicit configuration; no external
corpus is shipped or implicitly enrolled. Retrieval is designed to degrade when
unavailable, so a successful chat is not proof that memory was saved or recalled.

## Workflows and the Library

The Library holds apps, workflows and tools. The desktop can save and edit workflow
definitions and start staged runs with agent/model choices, budgets and owner gates.
Workflow definitions/runs have server storage; app Library entries also use local
app storage. Workflows and unattended runs are a preview: a run can pause or stop
before completing. Scheduled items become due with a Run action; they do not
start themselves in this release.

## Headless access

Source installs can run `bobclaw-core/mcp_serve.py` as a standard-input/output MCP
server. It calls the gateway with `BOBCLAW_AGENT_TOKEN` and an explicit
`BOBCLAW_GATEWAY` URL. Its catalog includes chat, memory and configured team
surfaces according to the token and deployment. Agent and portal tokens are
different credential types; neither is a general human login. Agent routes are
restricted by default, but the optional `BOB_AGENT_INNER_GATES=open` setting
relaxes agent restrictions and should not be treated as strict mode.
The loopback bridge is a different, unauthenticated compatibility surface;
see [the bridge guide](docs/operations/bob-bridge.md).
