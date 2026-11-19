# Packaged worker boundary

Execution modes, per-lane worker identities and the built-runtime acceptance harness,
implementing the S2d brief's shared Codex sign-in decision. S2e adds an explicit,
versioned existing-lane migration for the packaged runtime. Production deployment
stays in legacy mode; this command must never run there. See Validation below
for what has been proven.

The 4.3a/b candidate replaces child execution with negotiated worker protocol v2
and a durable shared lane queue. It adds a separate installed journal service;
raw v1 child execution cannot bypass scheduling. See
[WORKER-PROTOCOL-V2.md](WORKER-PROTOCOL-V2.md) for state placement, recovery,
compatibility, deviations, `--worker-protocol` and `--job-ladder --job-tier`
acceptance modes. The ladder adds 15/30/60-minute presets and reviewed finite
custom/extended limits, with core grant/accounting checks and an independently
enforced installed worker ceiling.
The older runtime receipts below do not validate these new changes.

## Third built-runtime findings (S2d5)

Option (a) keeps the host/abstract Unix socket boundary: use Chromium's headless
shell, which does not create full Chromium's profile singleton socket, and CDP
over inherited pipes. No AF_UNIX socket creation is opened up. Anonymous
socketpairs of all types remain allowed for browser IPC/crashpad.

The exact source recovered offline from the exported S2d4 image shows that
`@playwright/mcp@0.0.40` requires Playwright/core
`1.56.0-alpha-1758750661000`, but the image installed browsers with a separate
`playwright@1.55.0` CLI. It also unconditionally calls `injectCdpPort()` before
isolated Chromium launch; CLI/config options cannot turn that off. The image now
pins both Playwright packages to MCP's exact version and installs only its
Chromium headless shell (revision 1191) with required system dependencies.
`prepare_browser.py` checks the package versions, dependency layout, shell
revision and exact injection source, then replaces only the injection function
with a no-op. Unexpected input aborts the build. Playwright then supplies its
default `--remote-debugging-pipe`; no TCP debugging listener is requested.
The worker selects only
`/opt/bob-browsers/chromium_headless_shell-1191/chrome-linux/headless_shell` and
fails explicitly if it is unavailable. There is no full-Chromium fallback.

All three reviewer fixes remain: auth ancestors use search-only O_PATH dirfds
with O_DIRECTORY/O_NOFOLLOW while the final directory stays readable/fsyncable;
both worker services return EPERM for systemd-denied syscalls; anonymous
socketpairs are unrestricted by the child seccomp filter. The harness's `admin()`
default `cwd='/'` also remains. Tests cover the 0711 ancestor/non-final symlink,
final-directory fsync, unit EPERM settings, real Linux stream/datagram/seqpacket
pairs with host AF_UNIX creation still denied, browser selection, build adaptation
and rejection on pin/source drift, and the harness cwd default/override. POSIX
syscall/dirfd tests are skipped on Windows; actual browser launch and the
`unix.abstract`/host-socket denials remain rebuilt-runtime acceptance gates.

Built-runtime acceptance then showed the headless shell's zygote still needs a user
namespace even with `--no-sandbox`; the worker denies namespaces, so every renderer/GPU
fork aborted (`sandbox/linux/services/credentials.cc`). `mcp_runner.py` writes a per-job
Playwright MCP config in the job TMPDIR that adds `--no-zygote`.

## Second built-runtime findings (S2d4)

The policy helper explicitly sets both `/run/bob-workers` and its lane directory
to root:root 0711 on every start, using pinned no-follow directory descriptors.
This repairs existing 0700 directories and does not depend on the policy unit's
0077 umask or systemd's creation-only DirectoryMode. Both sockets remain 0600,
owned by the exact service UID; policy receipts remain root-owned 0644 and the
guardian socket remains worker-owned 0600. Worker-home parent directories also
get root:root 0711 so the worker can reach its private 0700 home. Traversal does
not grant directory listing or access to another lane's sockets.

Core startup takes a database-wide transaction advisory lock before running any
canonical schema DDL. Commit, rollback and connection loss release the lock;
the lock wait and schema command each have a 120-second timeout. An unpublished
pool is terminated on failure or cancellation. This fixes a **pre-existing risk
at 298ab41**: its core/db.py and start.py are identical to the pre-S2d4 candidate,
and every lane ran the same shared init.sql without serialization. The baseline
source contains AccessExclusiveLock-taking ALTER TABLE statements alongside
index/schema work; no baseline Postgres deadlock reproduction was run on Windows.

The fatal-startup hang came from the open aiosqlite connection's non-daemon
thread. Startup now explicitly cleans up after failure, including when aiohttp
has not completed startup and will not run its normal cleanup hooks. SQLite
migration failure also closes its connection; normal cleanup closes SQLite as
well as Postgres. Pool shutdown is bounded and terminates remaining connections.
The exception propagates and the process exits nonzero for systemd's restart.

All five lane service templates and both socket templates assert that their
identity drop-in exists. Their placeholder identity is `bob-unprovisioned`, an
account the image never creates. Missing provisioning therefore fails to start
without using systemd's special nobody user or silently using root.

Acceptance checks own exec/auth socket access and foreign-lane denial as **both
actual service users**, initially, after policy restart, and after reboot. A real
startup subprocess opens SQLite, injects a fatal Postgres error and must exit
nonzero within 20 seconds. Both cores restart together for the startup readiness
check. Child failures include stdout JSON and stderr in the parent diagnostic.

The full harness review also added preflight for runtime utilities and a script
location readable/traversable by the service identities. Root's PATH includes
sbin; denied-helper probes use resolved absolute paths so a missing command is
not accepted as permission enforcement. Per-call budgets cover the admitted job
timeout plus transport cleanup, and core-cases has a separate cumulative budget.
Host aliases are resolved by root and their numeric addresses receive real live
listeners; worker DNS failures cannot stand in for firewall evidence. Worker HOME
write access is exercised. Cancellation waits for the descendant PID receipt
before disconnecting, and descendant/policy teardown uses bounded observation
instead of fixed sleeps. The portal check waits for the actual bobapp broker
identity/exec and closes stdin for graceful cleanup, with a process-group backstop.

`test_runtime_fixes.py` adds offline schema-concurrency/error/cancellation checks,
real child-process startup failures at Postgres and graph compilation, directory
mode repair checks, identity-template checks and harness diagnostic/preflight
regressions. Its actual POSIX umask/mode test runs on Linux; the Windows test
checks both explicit descriptor mode/ownership operations across repeated starts.
These tests do not establish kernel enforcement or real Postgres lock behavior.

## Execution mode

`core.worker.mode()` is the single switch. It returns `worker` whenever
`/etc/bob/execution-boundary` exists, including an empty marker or a dangling
symlink; errors inspecting it also fail closed. Without a marker,
`BOB_EXECUTION_BOUNDARY=worker` opts into worker mode. All other environment
values leave an unmarked host in `legacy`. No environment value can turn off an
installed marker. Mode detection does not load lane, policy or worker files.

The Containerfile creates `/etc/bob` as root:root 0755 and writes the root:root
0444 marker. Core, gateway and bridge units also set
`BOB_EXECUTION_BOUNDARY=worker`. The marker remains authoritative even if an
EnvironmentFile overrides or removes that setting. Service users cannot write,
rename or unlink it. The acceptance harness checks the marker's ownership and
permissions, then attempts environment overrides and those file operations as
both real service identities.

Unmarked production deployment stays in **legacy mode**. Its migration is outside
this packaged-runtime release. Legacy branches restore `298ab41` behavior:
subprocesses and CLI health checks, spawn-context AGENTS.md and CLAUDE.md briefing,
Codex `-o` output files and its child-environment strip list, configured
MultiServerMCPClient, build subprocess/Docker selection, GUI/CDP, ledger Git and
lane-config Codex paths. Legacy execution does not load `/etc/bob` worker or
policy configuration. There is no automatic production migration.

Worker mode retains all S2d routing and refusals below. A missing lane worker ID,
worker service/configuration or network policy gives an explicit `unavailable`
reason. Excluded routes remain unavailable. These failures never select a legacy
branch. The AST inventory accepts direct execution only inside an explicit
`mode() == 'legacy'` branch (including its imported alias); the reviewed worker
executor, MCP transport and parse-only compiler are separately pinned. New
ungated execution sites fail the inventory test.

## Installed boundary

S3d adds a gateway/settings boundary for fresh and explicitly migrated lanes:
`bob-s-ID` retains core/dispatch authority, `bob-g-ID` owns gateway authentication,
`bob-b-ID` runs the bridge, and `bob-w-ID` remains the worker. This supersedes the
shared core/gateway/bridge identity described in the historical paragraph below.
Gateway/bridge env files now live outside the core-writable lane tree; gateway
SQLite is separately gateway-writable and core-read-only. Only the gateway UID
can use the root settings control socket. Bobapp has a separate read-only receipt
socket. Provider vault access belongs to core and root; neither gateway, bridge,
bobapp nor workers can read it. The schemas are in
`bobclaw-core/core/settings_contract.py`; `acceptance.py --settings` is the gate.
Unmarked legacy production is unchanged. Windows unit results do not establish
the installed Linux boundary.

Root journals a collision-checked 12-hex internal lane ID and persistent numeric
identities in `/etc/bob/lane-identities/`. Accounts are `bob-s-<id>` for
core/gateway/bridge and `bob-w-<id>` for execution. Accounts and UID mappings are
retained on removal. Interrupted account allocation and fresh provisioning are
journaled; retries preserve passwords, TOTP, grants and reserved ports.
`/var/lib/bob/lane-provisioning/` is root-private and contains setup secrets.
Existing lanes without that fresh-provision journal must use `bob-runtime
migrate-workers`; `bob-lane add` never adopts their files. New account allocation
explicitly reserves numeric UIDs in the persistent mapping before useradd, using
61000–64999 and excluding every retained mapping even if its account was deleted.

Root/systemd creates `/run/bob-workers/<id>/exec.sock`, mode 0600, owned by the
matching service UID, inside root-controlled directories. The worker inherits the
listener through socket activation. Accepted connections require the exact
service UID via `SO_PEERCRED`; the request cannot select a lane or identity. On
the client side `SO_PEERCRED` identifies **root**, the creator of the systemd
listener, rather than the worker that subsequently accepts it.

Installed profiles in root-owned `/etc/bob/workers/<id>.json` fix executables:

| Profile | Execution |
|---|---|
| `shell` | `/bin/sh`, offline |
| `python` | image venv Python, offline; workspace imports permitted |
| `git` | `/usr/bin/git`, offline, including hooks |
| `codex` | installed Codex CLI, leased lane proxy with fixed Codex HTTPS hosts and proxy-side DNS, lane Codex home |
| `mcp` | isolated installed Python controller; installed filesystem/browser servers over stdio |
| `auth-replace` | checked compare/replace of this lane's one Codex auth file; no child command |

The protocol requires version/job/profile/workspace/cwd/args/input/timeout/output
ceiling/expiry. It rejects unknown fields and UID, executable, environment, mount,
capability, fd, unit, socket and lane overrides. Workspace IDs resolve only through
the installed registry. Jobs have bounded input, a finite selected/effective
deadline within the installed ceiling (15/30/60-minute presets in 4.3b), and
4 MiB of combined output; stdout/stderr chunks and the terminal receipt are separate.

Environment starts empty: fixed PATH/locale, worker HOME/XDG and job TMPDIR; only
the Codex profile receives CODEX_HOME. No service/provider environment, proxies,
Python/loader paths, shell hooks, SSH agent, database connection or portal handle
is inherited. Listener and guardian descriptors are closed before child exec.

Systemd applies an empty capability set, no new privileges, no supplementary
worker memberships, umask 0077, private devices/tmp, protected proc/kernel/cgroups,
read-only system/code and bounded memory/tasks/CPU/fds. A private `/var/lib/bob`
mount exposes only this lane's projects, scratch and Codex home. `/run` exposes
only read-only firewall readiness/status mount holes; portal, database, Docker,
systemd and other worker submission sockets are hidden. Home and host mounts are
hidden. Child seccomp additionally denies creation of host Unix sockets (including
abstract sockets), io_uring, namespace/mount/debug/fd-steal operations, and all
Internet sockets for offline profiles. Anonymous socketpairs of any type remain
available (asyncio, and Chromium's crashpad/IPC needs SOCK_SEQPACKET); a pair is
connected only to itself. The worker units set `SystemCallErrorNumber=EPERM`, so a
syscall denied by systemd's filter fails with EPERM instead of killing the job.

The daemon is a Linux child subreaper. Cancellation is connection closure.
Timeout, cancel, output overflow **and normal completion** kill/reap all job
descendants using kernel PPid relationships, including double forks and new
sessions. Process-group kill is only a fast path. Cgroup-wide systemd cleanup is
the crash/restart backstop. Session creation remains possible for browser/CLI
launchers; it does not escape subreaper cleanup.

Known limitation / next slice: the daemon runs one execution job at a time per
lane. `kill_descendants` kills every daemon descendant, so concurrent jobs within
one lane are not supported. Per-job process ownership and cleanup must be
designed before enabling concurrency; this slice does not change that behavior.

## Boot ordering and lane availability (S2d3)

Both lane sockets are enabled under `multi-user.target`, with
`DefaultDependencies=no`. A default socket has `Before=sockets.target`, and
`sockets.target` precedes `basic.target`. The firewall policy is a normal service
after `basic.target` and `network-online.target`; waiting for it from an early
socket formed the observed boot cycle. Moving only `WantedBy` would not remove
the implicit ordering edge, so both sockets explicitly disable default
dependencies. They restore `Conflicts=shutdown.target` and
`Before=shutdown.target` for shutdown cleanup.

Boot proceeds from basic/network readiness to the root firewall guardian's
`READY=1`, then to the exec and auth sockets. Each socket requires, orders after
and binds to the policy. The sockets bind to each other without ordering against
each other, so either socket's failure/stop takes down both submission paths.
Both worker services bind to and order after both sockets and the policy;
processes start on socket activation, so an idle service may normally be inactive.
Policy loss also explicitly stops both sockets and both service cgroups in the
guardian's cleanup. Unsupported nft/DNS/kernel setup leaves execution explicitly
unavailable; no execution-mode fallback or automatic policy restart is added.

The root guardian uses `NotifyAccess=all`, allowing notifications from its own
service cgroup, including helper processes, instead of the observed rejection of
a non-main PID. This does not grant service or worker UIDs access to the root
guardian's cgroup. The image now installs `iproute2`, supplying the runbook's
`ip -4 route show default` command.

WSL can stop the distro after the last `wsl.exe` session exits. Every restart
therefore traverses this boot path; keeping one interactive WSL session open is
useful during a manual inspection. No host lifecycle setting is changed.

`--after-reboot` first observes every `/etc/bob/workers/*.json` lane, including
provisioned lanes outside the two disposable test names. It waits at most 60
seconds for each lane's policy service, exec socket, auth socket and policy
receipt to be active/present, prints their individual states and fails before
constructing the harness if any are unavailable. It starts/restarts no units in
that check, so harness setup cannot conceal a broken boot. A real boot-ID change,
retained identities and actual worker execution are checked afterward for both
disposable lanes. Repeat terminate/start/`--after-reboot` to test repeated WSL
boots; the successful baseline receipt remains available.

`test_runtime_boot.py` parses the shipped units, models service/socket default
ordering and rejects a default socket requiring/binding to/ordering after a
service that follows `basic.target`, including indirect ordering. Regression
cases restore the original cycle and check that it is detected. The unit suite
also checks shutdown ordering, paired availability, argument preflight and that
the boot probe inspects every provisioned lane before any repair.

## Shared auth and ownership

The worker owns `codex-home` (2750, worker group); the service is a member of that
group only to traverse/read that home and read/write `auth.json` (0660). It cannot
create or rename directory entries there. Enrollment runs Codex as the worker,
with no supplementary groups. Checked inode permissions are normalized after
login, while jobs run, and by the idle worker/auth publisher, because CLI refresh
may replace a file with mode 0600.

Device sign-in is a root setup operation, but its Codex child remains the lane
worker with no supplementary groups. Root obtains a `setup-codex` lease on the
existing egress control socket, verifies the egress UID, and injects authenticated
HTTP(S)/ALL proxy variables with empty NO_PROXY. The proxy requires kernel peer
UID zero and enforces the fixed Codex CONNECT hosts itself, not helper callbacks.
The lease lasts at most the remaining 15-minute sign-in deadline, without the
job grace period. Socket EOF or expiry revokes it and cancels tunnels; helper
cancellation, timeout or death cannot leave an independent renewable lease.
Worker jobs and models cannot issue or extend it. The firewall stays unchanged.
Key-file enrollment needs no network. Root account-model GETs and service HTTP
refresh keep their current network path. CLI refresh inside a job keeps its
existing job proxy lease. See BUILDER-NETWORK.md for the live boundary check.
Authenticated proxy support in Codex 0.166.0 device login and real sign-in
completion remain unverified until the owner runs the installed-runtime check.

POSIX reads walk ancestors without following symlinks, open relative to a pinned
dirfd with `O_NOFOLLOW|O_NONBLOCK`, and require a regular file, expected worker UID,
one hard link and a size limit. FIFOs, symlinks, hardlinks and foreign owners are
refused. The worker-writable `config.toml` receives the same bounded checks.

HTTP refresh uses a service-private lock and re-reads/adopts CLI rotations. Since
the unprivileged service cannot chown a replacement inode to the worker, atomic
publication goes to the independent root-created `auth.sock` and
`bob-worker-auth@<id>.service`, under the **same worker UID and restrictions**.
It accepts only the `auth-replace` profile, compares the original file digest,
creates an exclusive no-follow temporary file, fsyncs, and uses dirfd-relative
`os.replace`/renameat. It never follows a destination or temporary symlink.
Separate publication avoids waiting behind a long CLI job. CLI does not share
Bob's advisory lock: rotations are adopted and reuse failures re-read the file;
this does not claim a transactional lock shared with the CLI.

Accepted risk: every worker in a lane can read that lane's GPT grant. It cannot
read Bob signing/password/TOTP/database/portal credentials or another lane's
grant. HTTP and CLI intentionally share one rotating file; no second login or
provider key is installed on the PC.

After device sign-in, root setup reads the account's visible model names and
saves the account default for the HTTP transport only when no model is configured.
A failed list read leaves sign-in saved and asks the owner to choose a GPT model.
Root-only `bob-model openai-models NAME` is a read-only account GET without auth
refresh; `bob-model openai-set-model NAME SLUG` validates live visibility, publishes
up to 12 model names for pins/workflows and restarts core through the existing role
environment writer. These metadata commands add no worker or model authority.
The Codex coding agent still has no Bob workflow-tool adapter. The real sign-in
completion not yet run limitation below remains in force.

Service state remains service-owned; control/profile/unit files remain root-owned.
Project/scratch defaults grant the matching service access to worker output as
data. Generated workspace writers use pinned directories and atomic replacement;
core never imports those outputs. In worker mode core never reads a worker-planted project
CLAUDE.md or writes legacy spawn-context briefing files as the service user.
The CLI reads its own project instructions inside the worker. Qdrant and the
embedder have separate service accounts. The bobapp portal boundary is retained.

## Routing and network

Codex completion/version checks, generic shell/build/test jobs, generated imports,
pytest collection/execution, generated CLIs, Git patch/ledger operations and MCP
creation/calls cross the worker client in worker mode. There is no local subprocess fallback in that mode.
The legacy build mode string `subprocess` now means the worker adapter.
Claude alternate/PC, agy, kimi_cli, pi_code, Strata SSH, OpenCode remote execution,
Docker and direct GUI/CDP launch/attach are unavailable/refused. Explicitly
configured excluded backends remain listed with an unavailable reason.

MCP commands are installed at image build time; core cannot supply arbitrary
MCP commands, environment or transports. Filesystem calls use their scoped cwd.
Browser navigation/inspection uses `mcp__browser__sequence`: up to 16 tool calls
in one disposable session. MCP uses stdio, and the pinned Chromium headless shell
uses Playwright's inherited CDP pipe after the checked build-time adaptation
described above; no host CDP port or profile singleton socket is used. Chromium's
inner sandbox is disabled in this installed MCP server because the worker's outer
namespace/UID/capability/seccomp boundary supplies containment. Codex keeps its
CLI sandbox flags; the separate positive gate must verify their kernel support.

The image installs nftables. Each worker UID has a root-managed `inet` output
table: reject route-local, loopback, private, CGNAT, link-local, multicast and
non-public IPv6 destinations (including transition ranges); permit DNS only to
the discovered non-loopback upstream resolver and public TCP 443, then reject.
This covers dynamic lane ports and all local database/embedder endpoints. Offline
profiles additionally deny Internet socket creation. A root guardian checks live
kernel rules before each admission and monitors them during jobs. A changed or
missing table removes readiness and stops worker cgroups and submission sockets.
The guardian's only worker-facing operation is a fixed read-only status query.
Rules remain installed on lane removal; UIDs are not recycled.

WSL's runtime config disables Windows interop **and** automatic drive mounting;
the worker also cannot reach `/mnt`, WSL host sockets or binfmt controls. Project
data must be explicitly imported into the Linux project tree or supplied by an
operator-registered restricted mount there. `bob-project-folder` remains root-only
and grants its registry only to that lane's service group. No broad Windows
folder access, Docker membership, sudo/polkit exception or core root capability
is added.

WSL2/Linux must actually support systemd socket activation, SO_PEERCRED, UID/DAC,
mount namespaces, seccomp, cgroup v2 limits and nft UID filtering. These are
acceptance requirements, not claims from this PC run. This image uses the nft
branch; unsupported nft/kernel policy leaves execution unavailable. It does not
fall back to address-only systemd filtering or shared-UID execution.

## Build and acceptance

In a build WSL/Linux shell with Docker and Python 3, from this backend directory:

```sh
BOB_RUNTIME_WORKTREE=1 bash deploy/runtime/build.sh s2d-local
```

The explicit worktree option stages a narrow file allowlist in a temporary build
context, without git staging/committing. The default remains an archive of HEAD.
Both paths exclude tests (including this acceptance harness), personal task notes
and real secrets. Output is `dist/bob-runtime-s2d-local.tar.gz` plus its checksum.
The build fetches pinned release binaries/npm package versions; no build or
dependency download was performed during the offline implementation.

In PowerShell from this backend directory (use a fresh distro/install directory):

```powershell
wsl.exe --import BobS2D C:\WSL\BobS2D .\dist\bob-runtime-s2d-local.tar.gz --version 2
wsl.exe -d BobS2D -u root -- systemctl start bob-firstboot.service
Get-Content -Raw deploy/runtime/tests/acceptance.py | wsl.exe -d BobS2D -u root -- sh -c 'mkdir -p /var/tmp/bob-s2d; cat > /var/tmp/bob-s2d/acceptance.py; chmod 0644 /var/tmp/bob-s2d/acceptance.py'
$s2dHost = ((wsl.exe -d BobS2D -u root -- ip -4 route show default) -split '\s+')[2]
$s2dListener = Start-Job {
    $tcp = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, 38991)
    $tcp.Start()
    try { while ($true) { $connection = $tcp.AcceptTcpClient(); $connection.Dispose() } }
    finally { $tcp.Stop() }
}
try {
    wsl.exe -d BobS2D -u root -- /opt/bob/.venv/bin/python /var/tmp/bob-s2d/acceptance.py --create --host-listener "${s2dHost}:38991"
} finally {
    Stop-Job $s2dListener
    Remove-Job $s2dListener
}
```

`$s2dHost` above uses WSL NAT's Windows gateway. With mirrored/custom networking,
set it to an actual Windows host IP reachable from the distro. The host listener
contains no data or credentials. The harness requires a successful **root-side**
connection before testing worker denial; a host firewall, wrong address or closed
port fails that positive control. It never opens Windows firewall rules itself.
`--host-listener` is repeatable and also accepts `[IPv6]:port`. Plain Linux does
not require this Windows-host control; local/interface IPv4/IPv6 listeners are
always tested.

Before creating either lane, the harness parses every listener as a numeric
IPv4 or bracketed IPv6 endpoint, checks the mode/name combinations and both lane
paths, and confirms each supplied host listener is reachable. A WSL baseline
requires a listener. Invalid arguments, an existing second lane or a dead host
listener therefore cannot leave the first lane provisioned. `--create` applies
only to the baseline; reboot and real-provider checks are separate invocations.

The harness creates only its two disposable `s2d-test-a`/`s2d-test-b` lanes and
refuses pre-existing lanes with `--create`. It temporarily enrolls a root-owned
network probe profile, injects synthetic canaries, and drives the real socket as
the real service identity. It exercises kernel denials against live IPv4/IPv6
listeners, core adapters, auth/socket forgery, helpers, privilege, proc, portal,
Git hooks, pytest plugins, generated imports, MCP/browser startup, forged-token
submission by the trusted harness, Windows paths, cgroup limits and lifecycle.
Output is JSONL per probe and a nonzero exit on failure. A baseline pass prints a
pending reboot probe; it is not a complete release gate. Profiles/environments
are restored in cleanup; disposable lane data and synthetic grants are retained.
The HTTP-refresh probe uses a mocked provider transport but the **real** service
UID, private lock and worker auth-publication socket; it checks the new inode's
worker ownership. A base rerun refuses real provider grants instead of replacing
them with synthetic credentials. Use new disposable lane names after a real
sign-in if another full baseline is needed.

```powershell
wsl.exe --terminate BobS2D
wsl.exe -d BobS2D -u root -- /opt/bob/.venv/bin/python /var/tmp/bob-s2d/acceptance.py --after-reboot
```

After the owner explicitly approves the real sign-in/completion, separately:

```powershell
wsl.exe -d BobS2D -u root -- bob-model openai-login s2d-test-a
wsl.exe -d BobS2D -u root -- bob-model set-default s2d-test-a codex_code
wsl.exe -d BobS2D -u root -- /opt/bob/.venv/bin/python /var/tmp/bob-s2d/acceptance.py --codex-positive
```

That step uses normal `/api/chat` dispatch and independently observes Codex's
actual worker UID, CODEX_HOME and lack of service secrets via root `/proc`. Do not
advertise the CLI runnable if this step fails. The harness never automatically
reboots or performs this provider call. On plain Linux copy the script to the same
root-controlled readable location, run the same Python commands as root and
perform an actual reboot before `--after-reboot`.

Windows unit command (the available venv is **inside backend**; the brief's
parent-directory venv is absent on this PC):

```powershell
$env:PYTHONPATH="$PWD\bobclaw-core\tests"
.\.venv-win\Scripts\python.exe -X utf8 -m pytest bobclaw-core/tests/test_worker_browser.py bobclaw-core/tests/test_worker_boundary.py bobclaw-core/tests/test_worker_runtime.py bobclaw-core/tests/test_codex_code_backend.py bobclaw-core/tests/test_configured_catalog.py bobclaw-core/tests/test_openai_codex_auth.py bobclaw-core/tests/test_openai_codex_transport.py bobclaw-core/tests/test_runtime_model_configured.py bobclaw-core/tests/test_lane_backend.py bobclaw-core/tests/test_execution_mode.py bobclaw-core/tests/test_codex_code_backend_worker.py bobclaw-core/tests/test_configured_catalog_worker.py bobclaw-core/tests/test_runtime_model_configured_worker.py bobclaw-core/tests/test_runtime_boot.py bobclaw-core/tests/test_runtime_fixes.py -q -p no:cacheprovider -p roster_golden.windows_tmp --basetemp=.test-tmp/s2d5-final
```

The Linux unit gate should include these same fifteen files; POSIX ownership,
symlink/hardlink/FIFO/renameat and seccomp socketpair checks then run rather than skip. Kernel/runtime
acceptance is additional to these units. The AST inventory pins every spawn,
dynamic code-loading and indirect process-client site in core/api/tools; new
sites require explicit review, including import/alias forms.

## Validation (2026-11-03, Windows build PC)

- Linux unit gate (Ubuntu WSL): no new failures against the `298ab41` baseline
  (core 40 / gateway 2 pre-existing failures; pipeline 132/0).
- Built runtime `bob-runtime:s2d6-local` from this tree, imported as a throwaway WSL
  distro with two disposable lanes and synthetic canaries: **229/229 probes passed**,
  then **9/9 after-reboot probes passed** (`wsl --terminate`, boot-time worker
  units, socket access, retained identities). The distro was then unregistered.
- Not yet run: the separate `--codex-positive` real sign-in completion (needs the owner's
  OK), plain-Linux (non-WSL) acceptance, and the S2e existing-lane migration harness.

Built-runtime issues found and fixed during acceptance: execution routing applied
outside the packaged runtime (now a root-marker mode), a sockets.target/basic.target
ordering cycle, 0700 `/run/bob-workers` directories, a pre-existing cross-lane startup
DDL deadlock and a core that hung after a fatal startup error, O_RDONLY ancestor walks
for the shared auth file, SIGSYS instead of EPERM for systemd-filtered syscalls, a
stream-only socketpair rule that broke Chromium, full Chromium's AF_UNIX singleton
socket (now the pinned headless shell over CDP pipes) and the zygote needing a user
namespace (now `--no-zygote` through a per-job Playwright MCP config).

## Existing-lane migration (S2e, version 1)

This is a **packaged-runtime operator operation**, not a production deployment
upgrade. Neither installing code nor booting an unmarked host invokes migration.
The absence of the marker keeps the existing legacy branches. No provider calls,
Windows-mounted project imports, optional CLI enrollment or new routes are added.

`bob-runtime migrate-workers [--lane NAME] [--dry-run]` is separate from the
firstboot-complete marker. It serializes with lane allocation/removal, model
Settings/enrollment, project registration and token renewal via `/run/bob-lane.lock`.
The opt-in `bob-firstboot --migrate-workers` queues `bob-migrate-workers.service`
after firstboot, including when firstboot already completed. An in-place upgrader
can invoke the same operator command after installing the new runtime helpers.
The migration unit is not enabled automatically.

The root-private journal is `/var/lib/bob-worker-migrations/NAME/state.json`
(directories 0700, files 0600). Phases are `quiesce`, `identities`, `ownership`,
`credentials`, `verify`, `publish`, `completed`. The journal contains original
UID/GID/mode/device/inode metadata, the lane ancestor's original ownership,
helper-tree and shared-ancestor inventories, persistent identity, credential-remediation receipts
and verification evidence. Until publication completes it contains the **new
signing keys** for replay; final publication removes `rotation.jwt` and
`rotation.vouch`, retaining the `keys_published`/`portal_reissued` booleans.
Never copy an incomplete journal into tickets or test reports. Unknown
versions and changed inventoried inodes require operator inspection.

Quiescing stops renewal, every legacy lane sharing `bob`, the affected lane's
worker/auth sockets and cgroups, and the shared Qdrant/embedder services. It kills
remaining processes with the old UID (and, on replay, the lane's isolated UIDs),
then freezes the lane ancestor before walking data. **Even `--lane NAME` stops
other legacy lanes**: their shared UID could otherwise continue mutating the
tree. The journal lives outside the old bob-writable `/var/lib/bob`; after
quiescing, both that directory and `family` become root-owned 0711. Other legacy
lanes remain stopped with persistent pending gates until
individually migrated. Expect a shared-helper maintenance interruption.

Ownership transfer uses pinned dirfds, `O_NOFOLLOW`, inode comparisons, device
checks and mountinfo checks (including same-device bind mounts). Policy depends
on the tree: service state and shared helper trees strictly refuse symlinks,
hardlinked regular files and special files, identifying the offending relative
path without logging contents. Worker `projects`, `scratch`, `codex-home` and
`cli` preserve symlinks without following or descending them; their inventoried
device/inode is rechecked immediately before no-follow chown. FIFOs and sockets
receive the same entry-only chown without being opened for I/O. Device nodes
and mounted subtrees remain refused everywhere.

Worker hardlinked regular files are copied from the pinned source descriptor
to an exclusive temporary in the same directory. Only the copy receives ACL
cleanup and target ownership/mode; the shared inode's owner, mode and ACLs never
change. The copy is fsynced and its prepared device/inode journaled before
dirfd-relative atomic replacement, followed by directory fsync. Each break is
journaled, so a kill during copy leaves the original name intact and a kill
after replacement recognizes the completed copy on replay. Validated journaled
temporaries are resumed; unrelated new, replaced or missing inventory entries
fail closed, including links and broken hardlinks. A complete metadata inventory
is durable before the first chown, so interrupted transfers retain original
metadata. Small root-private `link-breaks/<path-hash>.json` receipts are atomically
saved/fsynced per break and merged on replay; they avoid rewriting the full
100k-inode inventory for every copy checkpoint. The final state contains the
completed break records too. An applied-tree checkpoint prevents retries from adopting files added
after a completed transfer. Access/default ACLs are removed with fd-based
`removexattr` (ENODATA/ENOTSUP accepted); only setting project/scratch directory
default ACLs uses `setfacl -m`. Regular-file setuid/setgid grants are removed;
only project/scratch sharing and root-parent worker traversal are
re-established. Service state goes to the service UID; only `projects`, `scratch`,
`codex-home` and `cli` go to the worker UID. Codex `auth.json` remains the existing
file/sign-in, normalized to worker ownership and 0660. Fresh provisioning and
migration share profiles, identity drop-ins, role envs and directory writers.
Qdrant/models move to their dedicated helper accounts; shared secrets become
root-private. Files outside these roots are not recursively adopted.

The marker and isolation-version requirement are monotonic. During migration,
core gets a `pending-...` worker ID, and the public worker config points at
nonexistent submission addresses. The actual root-created systemd listeners
can therefore be tested using the real service UID without making normal core
execution available. Verification checks the running service UIDs/readiness,
root-controlled marker, socket peer UID, actual worker UID/groups/capabilities,
live nft policy/guardian and a service-readable secret canary denied to a real
worker job. The completion-version receipt is fsynced **before** normal socket
addresses are published atomically. `activation_complete=true` records successful
publication. A kill before that final receipt causes re-quiescing/reverification
on replay; a completed second run changes no env, token or journal. Errors leave
the pending gate, unavailable core execution and stopped worker sockets/cgroups.

JWT and vouch keys rotate independently. The new values are journaled before
publication, and portal credentials are reissued by the existing trusted mint
path. A retry can reissue a portal token if killed between mint and receipt; it
never chooses another signing key. Password, TOTP and shared DB credentials are
preserved and may already have been exposed. The command prints a remediation
notice even if migration later fails.

### Operator runbook

1. Back up the runtime offline, including lane data, persistent UID mappings,
   provisioning/migration journals and operator portal credentials. Keep the
   backup private. Install this candidate's packaged Python helpers, wrappers and
   unit files through the normal image/upgrade path; do not run it on deployment.
2. Inventory lanes with `bob-lane list`. Review custom paths and removed lanes.
   `bob-runtime migrate-workers --dry-run` prints phase plans and makes no changes.
   It checks lane configuration/mapping compatibility; the full no-follow tree
   inventory happens after quiescing. Nonstandard configured data paths must be
   moved into the canonical lane layout first. Retained `.disabled` lanes are
   deliberately refused; do not remove that marker merely to bypass review.
3. During a maintenance window run `bob-runtime migrate-workers`, or select
   `--lane NAME`. This does not ask for a Codex re-login or reset user credentials.
   Complete all other stopped legacy lanes before returning normal service.
   For about 100k files, ACL cleanup now costs two xattr syscalls per ordinary
   inode rather than 100k–200k process launches. Directory default ACLs still
   launch one `setfacl -m` per project/scratch directory. Copy time depends on
   total hardlinked bytes; durable per-break journal writes also add time.
   There is no measured 100k-file runtime yet: budget the maintenance window
   using a disposable copy on the target filesystem, timing the command with
   the Bash builtin `time -p bob-runtime migrate-workers --lane NAME`. Record file and
   directory counts, hardlinked bytes and elapsed time; do not extrapolate from
   the small acceptance fixture or promise a fixed duration.
4. Check JSON phase receipts and the private journal's `phase=completed` and
   `activation_complete=true`; do not print signing-key fields. Inspect
   `systemctl status bob-core@NAME bob-gateway@NAME bob-bridge@NAME` and the mapped
   `bob-worker-policy@ID`, `bob-worker@ID.socket`, `bob-worker-auth@ID.socket`.
5. After interruption/failure, inspect `phase`, `failure` and inventories privately,
   resolve the actual unsafe path/policy/service problem, then repeat the same
   command. Do not delete journals, mappings, the marker or pending gate. Account
   allocation and partial ownership transfer resume with the same identities.
6. For service recovery/rollback use `bob-runtime suspend-workers --lane NAME`.
   This keeps service on its isolated UID with execution unavailable and preserves
   the version requirement. Re-run migration to verify/re-enable. Never roll back
   to shared `bob` execution, restore old signing keys, or install binaries that
   ignore the boundary marker. Data/ownership rollback to the shared UID is not
   supported.
7. Separately reset potentially exposed passwords/TOTP as root in the lane's
   `lane.env`, run `bob-runtime refresh-env --lane NAME`, restart that lane's
   services and re-enroll clients. Coordinate shared PostgreSQL rotation using
   `ALTER ROLE`, matching `POSTGRES_PASSWORD`/`POSTGRES_URL` in the private shared
   env, refresh every lane's role envs and restart them. Migration never performs
   these coordinated resets automatically.

For a disposable migration acceptance run, boot a **new, empty test distro with
the boundary marker removed before creating any lanes**. Install the harness at
a service-readable path such as `/var/tmp/bob-s2e/acceptance.py` (0644, ancestor
directories 0755), then run:

```sh
/opt/bob/.venv/bin/python /var/tmp/bob-s2e/acceptance.py --create --migrate-existing \
  --lane-a s2e-test-a --lane-b s2e-test-b --host-listener HOST_IP:PORT
```

The host listener is the same live positive control required by the WSL baseline.
The harness refuses an existing lane or marked runtime for this scenario. It
starts actual legacy lane services as `bob`, seeds synthetic shared Codex grants,
secret/data files and a registered project containing a Python venv, a
`node_modules/.bin` symlink and an offline `git clone --local` with hardlinked
objects. A scratch hardlink and symlink target B's service-state file. The harness
kills a real migration of A in the verify
phase, checks core's unavailable result, resumes, verifies preserved bytes/config,
checks B's inode owner/mode immediately after A migrates (before B's own migration),
checks rotation-secret removal, old JWT rejection with a new-JWT positive control,
and proves no-op replay. After both lanes migrate it runs the venv Python, cloned
repo's `git status` and `.bin` tool through A's real worker socket, verifies private
copies, and denies reads of B's file both directly and through the preserved symlink.
It then runs the full existing baseline probes on the migrated lanes. Use the
existing `--after-reboot` invocation afterward. No live provider completion is
part of this scenario.

S2C deviations are explicit: the owner's shared-sign-in decision replaces section 5
step 4; a bounded offline self-check precedes reopening, while the full denial
suite remains operator-run acceptance, and live CLI completion stays separate.
The shared-UID threat requires stopping all legacy peers even for a selected lane.
Windows unit tests do not establish Linux DAC/nft/systemd enforcement. The s2d6
Linux/runtime receipts above remain the baseline, not evidence for this migration.

S2e offline validation on the Windows build PC: **215 passed, 12 skipped** across ten focused
files (25 passed / 4 skipped in the new migration suite). Receipts are retained
at `.test-tmp/s2e-final-a2.xml` and `.test-tmp/s2e-final-b.xml`. The optional Windows
scratch-directory shim was used, as for the earlier unit runs. Linux/POSIX skips
do not prove traversal or kernel enforcement. No image build, Linux gate, WSL
migration harness or live provider call was run; `runs/gate-s2d6/` is unchanged.

S2e2 offline validation on the Windows build PC: **292 passed, 13 skipped** across twelve
focused migration/worker/runtime files, using `.venv-win` and the same optional
Windows scratch-directory shim. The migration suite contributes **55 passed,
5 skipped**, including simulated descriptor/link graphs and durable replay at
temporary creation, mid-copy, prepared-copy and post-rename crash windows.
The receipt is `.test-tmp/s2e2-final-verified.xml`. All changed Python files parse,
LF endings are verified and `git diff --check` passes. POSIX skips, the updated
WSL acceptance fixture and the 100k-file timing exercise still need the rebuilt
throwaway runtime; no image, Linux gate, network or provider call was run here.

## Migration validation (2026-11-03, Windows build PC)

Built `bob-runtime:s2e5-local` from the worktree and imported it as a throwaway
WSL distro `BoB-isotest`. The boundary marker was removed before firstboot, so the
distro behaves like a pre-boundary install. Ran
`acceptance.py --create --migrate-existing --lane-a s2e-test-a --lane-b s2e-test-b`:
**245/245 probes passed**, including all 16 `migration.*` probes and the full
baseline denial suite on the migrated lanes. After a distro restart,
`--after-reboot` passed **9/9**. The distro was then unregistered. No live provider
calls were made. The Linux gate matches the s2d6 baseline: core 40 failed / 4657 passed,
gateway 2 / 526, pipeline 132 / 0, no new failures.

The built-runtime runs found three defects that the unit tests had missed. All are fixed, with
regression coverage where a unit test can express them:

1. `/etc/bob/workers` was created with `mkdir(mode=0o755)` under the
   migration's `umask 077`, which left it 0700. Workers could not read their
   profiles and crash-looped, and verification timed out. `worker.provision` now sets
   owner and mode on the directory fd explicitly.
2. `publish` restarted lane services and reported `completed` before they
   accepted connections. It now waits for readiness (`wait_ready`) first.
3. Lane cores `Require=bob-qdrant`. Migrating a later lane stops the shared
   helpers, and systemd stopped every already-migrated lane with them without
   restarting them. After each lane's migration, success or failure,
   `restore_migrated` starts the completed lanes and waits until they are ready.

## Files changed

```text
bobclaw-core/core/backends/agy_code.py
bobclaw-core/core/backends/claude_code.py
bobclaw-core/core/backends/claude_pc.py
bobclaw-core/core/backends/codex_code.py
bobclaw-core/core/backends/kimi_cli.py
bobclaw-core/core/backends/local_strata.py
bobclaw-core/core/backends/openai_codex_auth.py
bobclaw-core/core/backends/opencode_pool.py
bobclaw-core/core/backends/opencode_serve.py
bobclaw-core/core/backends/pi_code.py
bobclaw-core/core/build/sandbox.py
bobclaw-core/core/build/skeleton.py
bobclaw-core/core/gui/escape.py
bobclaw-core/core/gui/surfaces/cdp.py
bobclaw-core/core/harness/sandbox.py
bobclaw-core/core/lane_config.py
bobclaw-core/core/ledger/gitdag.py
bobclaw-core/core/mcp/client.py
bobclaw-core/core/nodes/build_plan.py
bobclaw-core/core/nodes/cc_edit.py
bobclaw-core/core/worker/__init__.py
bobclaw-core/core/worker/auth_file.py
bobclaw-core/core/worker/client.py
bobclaw-core/core/worker/contract.py
bobclaw-core/core/worker/daemon.py
bobclaw-core/core/worker/mcp_runner.py
bobclaw-core/core/worker/restrict.py
bobclaw-core/core/worker/workspace.py
bobclaw-core/tests/test_codex_code_backend.py
bobclaw-core/tests/test_configured_catalog.py
bobclaw-core/tests/test_runtime_model_configured.py
bobclaw-core/tests/test_worker_boundary.py
bobclaw-core/tests/test_worker_browser.py
bobclaw-core/tests/test_worker_runtime.py
bobclaw-core/tests/test_runtime_boot.py
deploy/runtime/Containerfile
deploy/runtime/WORKER-BOUNDARY.md
deploy/runtime/bin/bob-embedder
deploy/runtime/bin/bob-project-folder
deploy/runtime/build.sh
deploy/runtime/config/wsl.conf
deploy/runtime/lib/common.py
deploy/runtime/lib/lane.py
deploy/runtime/lib/model.py
deploy/runtime/lib/worker.py
deploy/runtime/lib/worker_policy.py
deploy/runtime/prepare_browser.py
deploy/runtime/snapshot.py
deploy/runtime/systemd/bob-bridge@.service
deploy/runtime/systemd/bob-core@.service
deploy/runtime/systemd/bob-embedder.service
deploy/runtime/systemd/bob-gateway@.service
deploy/runtime/systemd/bob-qdrant.service
deploy/runtime/systemd/bob-worker-auth@.service
deploy/runtime/systemd/bob-worker-auth@.socket
deploy/runtime/systemd/bob-worker-policy@.service
deploy/runtime/systemd/bob-worker@.service
deploy/runtime/systemd/bob-worker@.socket
deploy/runtime/tests/acceptance.py
```


## CC1: packaged Claude subscription setup

The claude profile uses the existing lane worker UID/GID, fixed
/usr/local/bin/claude, empty-derived environment, private home and explicit
CLAUDE_CONFIG_DIR at lane/cli/claude. It requires the lane proxy lease, UID
firewall, seccomp and Landlock. Contract, proxy environment, profile-network,
daemon writable grants, fresh provisioning, upgrade hooks and readiness include
this profile. Queued jobs still accept only --version/--help; auth is exclusively
root setup so a pasted code never enters the durable worker-job journal.
CC2 turns use the separate private Claude supervisor below. Only claude_code is
enrolled on that path; claude_pc and claude_alternate stay excluded.

Root setup shares the Codex lease implementation, admitting setup-claude only
from UID 0, with a fixed matched Claude profile and a maximum 900 seconds.
Config and .credentials.json stay worker-owned/private (0700/0600); the core
account gets no credential-read grant. Checked reads pin no-follow ancestor FDs
and require a regular inode, expected UID, one link and <=1 MiB. Enrollment is
root-published sanitized status plus an explicit declared model choice. It is a
subscription provider, tool-capable through CC2 when its isolated turn socket is
ready, independent of claude_api.

Both sign-ins discard stale fixed-pattern private homes under the shared lane
sign-in lock. SIGTERM, SIGINT, SIGHUP and the Claude native input pipe's EOF
cancel with cleanup; an uncatchable kill is recovered by cleanup at the next
sign-in, never by publishing a leftover home. Which signal reaches the Linux
helper when the native app kills wsl.exe remains an installed-runtime test;
Windows process termination alone does not establish that observation.


## CC2: Claude uses Bob tools

The root-created bob-claude-turn@<worker-id>.socket admits only that lane's core
UID (0600 plus SO_PEERCRED). Its independent hardened worker service uses the
same Claude profile, UID firewall, fixed host policy, seccomp and Landlock. It
has no project/scratch/Codex-home mount. Each turn gets a non-dumpable subreaper,
process group, PrivateTmp directory and two 0600 FIFOs for a fixed stdio MCP shim.
Other confined jobs have neither the turn directory's Landlock grant nor this
service's PrivateTmp mount. /proc fd reopening cannot add a grant; ptrace,
process_vm and pidfd fd theft remain denied. A bare same-UID process outside the
worker confinement is not an authorized task and is not the threat boundary.

The shim owns no credentials or dispatch capability. Core lists the exact
filtered tools and converts its calls into the existing _run_tool_loop path.
Workflow before_tool, Builder modes/approvals, PC authority, questions, receipts,
refusals and budgets are unchanged. A separate model supervisor avoids holding
the single worker queue while its Bob tools need that queue. Several calls from
one reported assistant message share that model round's charge; the final tool
result is held until Bob authorizes the next round. CLI-internal provider retries
are opaque and not claimed to be independently counted.

All tools and hooks built into the CLI are disabled by fixed argv/settings;
--bare is deliberately absent because it disables subscription OAuth. Only the
Bob MCP server is configured. Where init supplies a tool list, an unexpected
surface fails closed. MCP_TOOL_TIMEOUT=86400000 gives long approvals/questions
room; the installed CLI's handling of that environment setting and safe-mode's
interaction with explicit MCP still require the pinned-binary live checks.

Core keeps full Bob history explicit in the input, no CLI resume/session files,
streams text deltas (never thinking), and records append-only private turn JSONL
under the lane's claude-turns directory before dispatch and after completion.
Model selection, first observed full name, usage and auth uncertainty are stored
without credentials or dollar spend in claude-runtime.json. Normal completions use
result usage. Bob-ended/interrupted invocations retain assistant-event usage as
partial when available, or explicitly unknown; they never invent zero usage. Missing end markers
mean interrupted/unknown, never permission to replay a tool. Normal Builder/PC
worker receipts remain the effect oracle. A verified new sign-in generation
clears stale auth uncertainty; a model selection cannot clear it.

Stop or controller EOF kills/reaps this turn's descendants before idle_proved.
A crashed supervisor forces its parent service to exit so systemd kills the
entire cgroup before restarting. Proxy leases end with the turn. The candidate
has scripted/offline tests, but no executed Python or installed-kernel acceptance
is claimed from the process-restricted editing sandbox. Run the CC2 report's
commands and live checks before release.
