# Security

BoB 0.99.1 is intended for one owner controlling the installation. Lanes separate
logins and files, but share backend services and database credentials. They are
not a security wall between people who do not trust each other. Keep BoB on
loopback and use SSH for the desktop app's remote connection.

## Listeners and remote access

| Installation | Default listeners |
| --- | --- |
| Windows installer / runtime image | Each lane has loopback core/gateway/bridge ports, initially 7835/7836/8902; subsequent lanes receive unused ports. Shared Postgres 5432, Redis 6379, Qdrant 6353/6354 and the optional embedder 58081 bind loopback. Worker/control sockets are local Unix sockets. |
| Standalone Ubuntu, native or Docker datastores | The supplied source setup starts core/gateway on loopback 7835/7836. Postgres 5432, Redis 6379 and Qdrant 6353/6354 are loopback services or loopback-published containers. It does not provision the image's bridge/workers. |
| Manually launched code | Bare core/gateway configuration still defaults to loopback 7825/7826. The optional Claude pipeline defaults to loopback 7823. Match client URLs to the actual launcher settings. |

Never expose core, the bridge, the optional pipeline, datastores, the embedder or
worker/control sockets to another device. Core does not authenticate its whole
HTTP API. It verifies an HMAC signature before accepting a forwarded scope, but
that check does not make a direct core connection safe. The bridge has no
authentication at all. Loopback also does not protect against another process
already running on the same host.

In the desktop app, **Connect to a Bob server** runs a server-side broker through
SSH, with `BatchMode=yes` and `StrictHostKeyChecking=yes`. Verify the server
fingerprint and configure key authentication first. For example, connect to
`example-server` using the broker launcher created by [AGENTS-SETUP.md](AGENTS-SETUP.md).
The broker accepts only a loopback gateway URL. Its portal bearer stays on the
server, so the gateway does not need a network listener for desktop access.

An SSH account that can read the standalone deployment's environment/token files
is trusted with that deployment. Runtime administration, root access and access
to the WSL distribution or its backups are also trusted. These controls do not
protect secrets from the machine administrator.

## Sign-in, tokens and lane files

The normal gateway login uses `BOBCLAW_PASSWORD` and the authenticator's TOTP code.
The gateway compares the configured plaintext password; it does not read
`BOBCLAW_PASSWORD_HASH`. Installed lanes keep password/factor state in a root-owned
credential journal. Startup validates required secrets, and TOTP checks reject
already consumed time steps. Credential changes have their own generation checks.

Human login returns a signed, expiring access token and a rotating refresh token.
The desktop broker instead holds a scoped portal bearer. A portal bearer is not
a human session and does not itself authorize owner-only execution decisions;
it does allow the broker's permitted lane reads/chat. Treat possession of the
bearer and access to the broker's operating-system account as sensitive access.

Owner operations use a separate short-lived session tied to the portal identity,
broker link and credential generation. New runtime lanes use `signed_in_user`
work confirmations: ordinary work approval does not ask for a fresh password and
authenticator code each time. Changing the approval policy requires those factors;
password/factor changes use separate factor checks. The lane password does not
encrypt its files or replace operating-system access controls.

Runtime provisioning assigns lane-specific service and worker users and private
directories. Each lane has separate SQLite memory/authorization state and a
Qdrant collection prefix. However, core and gateway for all lanes receive the same
Postgres credentials and Redis URL. Row ownership checks are application logic,
not separate database accounts restricting each lane's rows. A compromised trusted
service is outside the worker filesystem boundary. Do not use this release as a
public multi-tenant service.

## Builder permissions

These modes describe the desktop Builder with the packaged worker services:

| Mode | What can happen |
| --- | --- |
| Suggest | Returns proposed mutations/commands instead of running them. Workspace reads remain available. |
| Approve | Recognized read-only commands can run; changes and other commands ask unless covered by an existing bounded task/session permission. |
| Auto | Workspace writes/edits, recognized read-only commands and a small test-command set can run. Other commands and workspace changes ask unless already granted. |
| YOLO | Requires an explicit risk acknowledgement and expires within the current session, at most two hours; it then returns to Auto. It permits broad workspace actions and network policy without individual prompts. |

Changing a Builder mode and granting an approval require an owner session.
Workspace creation/mounting asks outside YOLO. Direct delete/restore operations
cannot inherit a session allowance, although Auto/YOLO policy can authorize
mutations and an approved shell program can itself delete files. No mode is a
proof that a command is harmless.

Windows PC access always needs a separate native grant or approval in the app.
The native PC modes can authorize later actions within that grant's task/folder
and lifetime; a fresh click is not required for every action. Protected path and
command checks can still force approval, including in PC YOLO. Command screening
is best-effort inspection, not a sandbox for arbitrary Windows programs.
A server-side Builder mode cannot create this native grant.

The source setup does not provision the desktop Builder's worker boundary.
Its `BUILD_SANDBOX=docker` setting applies to the older contract-build verification
path. Explicit Docker mode fails if the daemon/image is missing and uses a
read-only generated-workspace mount, no network, dropped capabilities and resource
limits. On a standalone host, `BUILD_SANDBOX=auto` can fall back to unisolated host
execution; `subprocess` is also host execution. Keep explicit Docker mode for that
path and do not treat it as a replacement for the runtime worker.

## Worker isolation and its limits

A packaged worker uses a dedicated unprivileged lane user, no supplementary groups,
no Linux capabilities, systemd filesystem/device/process restrictions and resource
limits. Before job code starts, Landlock ABI 3 or later restricts file access to its
selected workspace, private temporary directory and permitted runtime/input paths;
syscall filters add restrictions. Missing required filesystem confinement refuses
execution. Subscription profiles deliberately receive access to their own lane's
CLI sign-in directory. Ordinary Builder commands do not get that extra directory
or the service's inherited provider-key environment.

Firewall rules restrict the worker user to its lane's egress proxy. Jobs without
networking additionally block connection syscalls. The proxy rejects private or
reserved destination addresses and limits traffic to HTTP 80/HTTPS 443. Builder
network policy combines built-in domains, project additions and owner grants.
Requests outside that policy normally need a decision; YOLO and matching grants
can authorize them. An HTTPS CONNECT tunnel exposes the host, not encrypted
request methods, paths or contents. An allowed destination can receive data from
the workspace. The allowlist is not a guarantee against exfiltration or malicious
packages, and these layers do not protect against a kernel or administrator compromise.

## Secrets on disk

The credentials below are plaintext files or fields in local state, not an encrypted
vault. Backups and WSL exports contain them too. Keep them out of version control
and restrict access to backup destinations yourself.

| Route / material | Location and intended Linux permissions |
| --- | --- |
| Runtime shared datastore credentials | `/var/lib/bob/secrets/bobclaw.env`, 0600. |
| Runtime initial lane settings | `/var/lib/bob/family/<lane>/bob-memory/lane.env`, root-owned 0600 after split-role provisioning. Core's generated `core.env` is root-owned 0640 for the lane service group. |
| Runtime active password/TOTP generations | `/var/lib/bob-settings/<lane-id>/credentials/`: secret generation JSON and `current.env` are root-owned 0600. The public generation pointer is 0644 and contains no password. Gateway service environment files in `/etc/bob/service-env/` are root-only 0600. Initial settings can retain older factor material; protect the whole state tree. |
| Runtime provider API keys | Six non-OpenAI presets use `/var/lib/bob-settings/<lane-id>/vault/`, directory 0750, key generations 0640 root/lane-service group. Legacy enrollment can use lane `providers.env` (0640). OpenAI key/sign-in state is in `codex-home/auth.json`. |
| Runtime ChatGPT sign-in / OpenAI key | Lane `codex-home/` is worker-owned 2750; checked `auth.json` is 0660, shared with the lane core service. |
| Runtime Claude sign-in | Lane `cli/claude/`, worker-owned 0700; published `.credentials.json` is 0600. |
| Runtime broker bearer | `/var/lib/bob-portal/<lane>.token`, 0600 owned by `bobapp`, inside a 0700 directory. |
| Standalone source setup | Checkout `.secrets/bobclaw.env` and `/var/lib/bob-source/family/admin/bob-memory/lane.env`, 0600 owned by `bob-source`; service home and `.secrets` are 0700. Optional provider keys belong in private lane `providers.env`; CLI sign-ins, if manually configured, use lane-selected homes. |
| Standalone tokens | Lane `broker.token` and checkout `.secrets/tui-token.json`, 0600. The TUI cache holds the human access/refresh pair; gateway SQLite holds server-side token/authorization state. |

These are provisioning modes, not a promise that copied files or manually installed
vendor CLIs keep the same permissions. Unix mode bits do not establish a Windows
ACL policy. Keep the WSL storage and exported backups under an appropriately
restricted Windows account.

## Installer and updates

The Windows installer is unsigned. Download it from the public repository's
release assets. Before importing the bundled runtime into WSL, the app checks its
SHA-256 against bundled metadata and refuses a mismatch. That detects damage or
a mismatch; it is not publisher authentication if the installer and metadata
were both replaced.

Before a new runtime upgrade, the app exports a pre-upgrade WSL backup and records
its hash. Resuming a pending upgrade retains that backup. A successful export or
matching hash does not prove restoration has been tested. Preserve a separately
tested backup before relying on rollback; see [UPGRADE.md](UPGRADE.md).

## If you deliberately publish a gateway

Desktop SSH access does not require this. For another client that needs HTTP or
WebSocket access through a reverse proxy:

- Keep the gateway bound to loopback behind a TLS-terminating proxy. Forward only
  its API/WebSocket routes; keep all internal services inaccessible.
- Use strong `BOBCLAW_PASSWORD`, TOTP and signing secrets; protect environment,
  token and backup files. Keep core/gateway `BOBCLAW_SECRET` values consistent
  so scope signatures verify.
- Keep `TRUST_X_FORWARDED_FOR=false` unless exactly one trusted proxy is directly
  in front, direct gateway access is blocked, and that proxy sets/appends the
  actual peer address. The resolver uses the rightmost header entry. This is
  not a general trusted-proxy chain implementation.
- Review limits and token lifetimes. The general request limiter is in-memory
  per process; adding gateway processes multiplies its effective capacity.
  Packaged defaults allow more requests than bare gateway defaults. These
  settings are not shared abuse protection for a public service.
- Test authentication, WebSocket forwarding and denials with your actual client.
  There is no gateway-hosted browser UI, and the desktop broker refuses non-loopback
  gateway URLs.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Open a
GitHub **security advisory** on the repository (Security → Report a vulnerability),
and we will coordinate a fix and disclosure.
