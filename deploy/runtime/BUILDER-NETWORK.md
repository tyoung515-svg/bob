# Builder bounded network

Provisioning adds a retained per-lane egress UID and `bob-egress-proxy@<id>`.
The worker firewall permits **only TCP 127.0.0.1:<egress_port>**; it permits no
direct DNS, HTTPS, IPv6 or LAN egress. Codex/MCP use their own job leases through
the same proxy, with fixed policies and no network approvals. Provider calls in
core are unaffected. Offline profiles retain seccomp's connect denial.

Builder shell jobs and Codex/MCP receive HTTP(S)_PROXY (upper/lower case),
ALL_PROXY and empty NO_PROXY, via random short-lived job credentials. Programs
which ignore proxy variables fail closed. npm audit is disabled. There are no
automatic upload hosts: ordinary package downloads use GET; publishing/audit
uploads need an explicit method approval. One shipped data file is authoritative:
`core/builder/network_allowlist.json`. Project owner extras add exact hosts, with
optimistic revision and mutation-ledger replay; wildcards are not supported.

`profiles.codex` separately permits only HTTPS CONNECT to `api.openai.com`,
`chatgpt.com` and `auth.openai.com`. The packaged native `gpt` profile is created
by `deploy/runtime/lib/common.py:ensure_codex_home` with `model_provider="openai"`;
the repository's matching sign-in transport identifies the ChatGPT endpoint in
`core/backends/openai_codex.py:BASE_URL` and refresh endpoint in
`core/backends/openai_codex_auth.py:TOKEN_URL`. `api.openai.com` covers native
OpenAI API-key mode. Plain HTTP, arbitrary configured provider base URLs,
subdomains and Builder package/project hosts are denied without approval cards.
This change does not make or verify a live Codex provider call.

Root setup device sign-in now uses the same proxy through a separate
`setup-codex` enrollment on its control socket. The proxy checks kernel peer UID
zero, requires the Codex profile, rejects extra policy fields, and enforces the
fixed `profiles.codex` CONNECT host list itself. The helper checks the proxy's
egress UID before enrolling. Its non-inheritable control socket lives only for
the device-login subprocess, with expiry no later than the original 15-minute
login deadline and no job grace period. Closing it, helper exit, cancellation,
SIGTERM, SIGKILL or expiry revokes the credential and cancels active tunnels.
Revocation after EOF is observed on the proxy's 0.1-second monitor tick. A worker
or model cannot enroll, renew or supply a policy for this root-only lease. No
firewall or worker-account permission is broadened. Codex still runs under the
lane worker UID/GID, and `share_auth` still hands over the same stored file.

`codex login --with-api-key` stores stdin locally and keeps its existing worker
account and minimal environment. Setup completion and `openai-models` /
`openai-set-model` use root's existing HTTP account GET, with no auth refresh;
they do not inherit the worker network restriction. The core HTTP transport's
refresh runs as the service account, while a CLI job's refresh uses its normal
Codex job lease. These paths are unchanged.

Codex 0.166.0 device-login support for authenticated HTTPS_PROXY remains a live
runtime check. Run `bob-model openai-login setupwin` as Linux root after copying
the changes and restarting the proxy. The real sign-in completion has not been
run in the Windows sandbox. A separate root-only Linux boundary check without
sign-in is `deploy/runtime/tests/bu9_network_acceptance.py --lane setupwin`, using
the installed runtime Python. Its full invocation is in the BU9 report.

The MCP job contract (`core/worker/mcp_runner.py:main`) enrolls only the local
`filesystem` and `browser` stdio servers, with no configured remote MCP URLs;
`profiles.mcp` is therefore empty. Browser tool arguments are not remote-server
configuration and cannot add allowed hosts. Public web browsing remains denied.
Remote MCP transport is not implemented by that runner; adding it requires an
enrolled server configuration/contract and a proxy-capable transport. Chromium
and Node do not universally honor proxy environment variables, so environment
injection alone is not evidence of functional remote browsing/MCP. No direct
egress or fallback is enabled for programs which ignore the proxy.

`Transport.submit` enrolls Codex/MCP leases only at actual queue dispatch, so
queue waiting does not consume their expiry. A peer-checked `network_proxy`
transport field carries the credential outside the frozen request/digest; the
daemon validates the exact lane loopback URL and installed profile before
launching. Job stdin/argv are unchanged. Tokens in terminal stdout/stderr are
redacted before daemon journaling and core receipt storage, including split
pipe chunks. Revocation/drain precedes completion. Each request has a profile,
job ID, domain, visible method, byte counts and decision in the proxy journal
and `network_log` in the owner-scoped durable worker receipt (not Builder's
conversation-specific `builder.network_log` operation). If core dies before
settlement, the proxy journal remains the request evidence.

Core enrolls the token through the Unix control socket after verifying the proxy
UID. The proxy verifies core's SO_PEERCRED service UID. Other worker profiles
cannot enroll or guess a token. Revocation drains/cancels tunnels and acknowledges
durable log callbacks before the tool receipt returns. Tokens expire within the
command timeout plus 20 seconds and disappear if core disconnects/restarts.
Codex/MCP lease ceilings follow the root-installed worker maximum timeout;
Builder retains its 660-second admission ceiling.
The token is never a provider credential, never returned in tool stdout/stderr,
and never part of a network log. Do not print worker request stdin in diagnostics.

The proxy listens only on IPv4 loopback, permits HTTP on port 80 and CONNECT on
443, resolves destinations itself, rejects every private/reserved/transition
address and pins the validated numeric IP when connecting. Even yolo cannot
reach internal services. Responses close each HTTP connection; request chunking,
upgrades and Expect are refused. Headers, request counts, concurrency, bytes and
idle time are bounded. No TLS interception is performed: CONNECT exposes the
host only, **not the method or inner TLS hostname**. Plain HTTP method limits and
read-only host approvals are best effort for TLS tunnels, as specified by the
network addendum. A public allowed host can receive HTTPS uploads. Redirects to
another host require a new proxy policy decision.

Unknown domains/methods immediately receive a 403 with `bob-proxy: domain ...
needs approval (approval_id ...)`. The pending server record appears in
`builder.mode.get.approvals` and the completed shell result's `approvals` array.
An owner can allow once, for the session, for the project (read/CONNECT), or deny.
The model may retry after approval; network approval never re-executes a shell
command by itself. Single approvals bind domain + visible method + command and
current session; session approvals bind domain + visible method.

Wire additions: `builder.mode.get.network` is `{allowlist,project_extra,revision}`.
`builder.network.set {project_id,add?:[],remove?:[],expected_revision,request_key}`
is owner-only (signed-in click confirmation), initial revision 0. It returns
`{project_id,allowlist,project_extra,revision,operation_id}`.
`builder.network_log {conversation_id|job_id}` accepts exactly one scope and
returns `{items,truncated}` (newest 500). The job_id is the shell's immutable
Builder approval ID, also returned on the tool receipt. Each log includes the
optional network approval_id, domain, method, bytes_in (upstream to job),
bytes_out (job to upstream), decision and timestamp. Every authenticated request
is also journaled by the proxy, including when core disconnects before a DB save.
Unauthenticated attempts are journaled without a job association.

Install with the existing runtime provisioning/migration path; no live deployment
is performed by this change. `migrate-workers` reopens the quiesced migration for
completed pre-proxy lanes missing an egress identity, retaining existing UIDs.
It also reprovisions S1b lanes whose Codex/MCP profiles lack the proxy marker.
Linux acceptance must prove nftables, systemd,
seccomp/Landlock, peer UID rejection, process cleanup and multi-lane separation.
The portable tests use loopback fake upstreams and do not prove kernel isolation.


CC1 adds a separate claude profile and root-only setup-claude lease through the
same setup_cli_env implementation used by setup-codex. Its exact CONNECT hosts
are api.anthropic.com (requests), claude.ai (account authentication), claude.com
(browser sign-in entry) and platform.claude.com (OAuth exchange/refresh/revocation).
These uses are documented in https://code.claude.com/docs/en/network-config.
No optional telemetry, download, connector, SSO, plugin or wildcard host is
admitted. CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, DISABLE_AUTOUPDATER=1 and
ENABLE_CLAUDEAI_MCP_SERVERS=false apply to every launch. The installed proxy's
sanitized domain/method/decision receipts must still be inspected in the live
sign-in; no claim that the pinned CLI's network behavior was observed here.
