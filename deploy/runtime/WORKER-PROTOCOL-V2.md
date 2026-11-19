# S43b–S43c / 4.3a–4.3b: durable worker protocol, queue and job limits

Candidate implementation, 2026-11-03. No provider, network, deployment or commit
operations were performed while implementing this slice. The existing Linux
gate receipts under the sibling `runs/` directory are unchanged.

## Protocol and installed state

Each execution/control connection first sends `{"version":2,"op":"hello"}`
and reads a v2 hello. It advertises the daemon epoch, highest persisted lane
generation, active execution, installed profile hashes and unchanged limits:
900 seconds, 4 MiB combined output and 1 MiB requests. It then sends one
`submit`, `inspect`, `cancel` or `idle_barrier`. Controls can be served while a
child executes. Observer closure has no execution ownership.

Core creates the execution ID during durable admission. Dispatch commits its
canonical wire digest, controller/generation, epoch and deadlines before IO.
Submission expiry is at most 20 seconds, separate from the execution deadline.
The worker enforces both the installed limit and `not_after` using a monotonic
deadline fixed before child creation. Requests above 900 seconds are rejected.

The daemon journals admission, execution intent, cleanup and terminal receipt.
It kills/reaps descendants on every exit path before persisting the terminal
receipt; normal parent exit also ends the job when detached descendants still
hold its output pipes open. Bounded output is retained for lost-reply recovery.
Same ID/digest recovers history; changed content conflicts. Permanent dedup
tombstones prevent a missing execution/receipt row from becoming a fresh launch.
Missing/corrupt whole history fails closed and is never silently initialized.

Lower generations and different controllers at the same generation are rejected.
A higher generation fences prior execution, requires cleanup and invalidates the
old idle barrier. Submission requires a barrier for the current generation and
epoch. Cancellation names the exact execution and controller; stale IDs cannot
cancel another job.

Raw v1 child execution is disabled on the execution socket. Existing Python
callers retain the explicitly documented v1-shaped `request`/`exchange` adapter;
it always admits a v2 queue operation. The separate v1 `auth-replace` socket
remains a no-child operation outside the execution slot.

### Why there is a separate journal service

The daemon and its children share a UID. A 0700 directory owned by that UID
would not protect history from a child. Root provisioning now installs:

* `bob-worker-journal@ID.socket` and `.service`;
* `/var/lib/bob-worker-journals/ID/journal.sqlite3` with WAL and synchronous FULL;
* a provisioning sentinel preventing automatic recreation after history loss;
* `journal_socket`/`journal_path` in the root-owned worker configuration.

The executor and auth service hide the journal directory with systemd
`InaccessiblePaths`. The executor sees only its root-created journal socket.
Existing child seccomp denies Unix socket creation, descriptor theft and ptrace.
The journal helper and both daemons disable dumpability to deny same-UID
`/proc/PID/root`, `/proc/PID/fd` and memory access. Child seccomp also denies
direct, thread, queued and broad signals targeting the deadline supervisor;
ordinary job-child signal management and anonymous socket pairs remain allowed.
Cleanup kills/reaps before cleanup/terminal journal IO, so persistence failure
cannot keep children alive during cleanup.
The helper has no execution RPC.
The service principal cannot connect directly to its 0600 worker-owned socket.
Only root provisioning creates new history. Retained histories are not deleted
when a lane is removed. Auth publication has no journal dependency.

This is a necessary 4.3a storage boundary; it does not implement 4.3c workspace
isolation or Landlock. Existing isolated installations need the new installed
units, configuration and root provisioning while quiesced. An old boundary
configuration without the journal fields is unavailable, with no v1 fallback.
This slice does not add a live upgrade command for already-isolated lanes.

## Shared queue, authority and recovery

`core.worker.client.exchange` and `execute_async` use the same background
asyncpg queue bridge. Sync build/Git adapters and async MCP/Codex adapters enter
that path without acquiring a second slot. Existing excluded transports remain
excluded. Legacy callers remain on their existing legacy branches.

Postgres stores attempts, append-only events, dedup records, caller groups,
queue tickets and the lane slot. A session advisory lock is the physical
controller lease; every takeover gets a durable lane generation independent of
run generations. A lost DB connection does not prove that the worker is idle.
Only a current-generation idle barrier or validated post-cleanup terminal receipt
can release the physical slot.

Scheduling is FIFO within a group and round-robin across ready groups. Admission
is bounded to 32 outstanding attempts per principal and 256 queued lane attempts.
Ordinary tools group by the authenticated principal, so opening conversations
does not create priority. Older internal callers without a vouched principal
share one conservative `lane-service` principal/group. Runs use their durable
root identity and generation, checked at admission, dispatch and cancellation.
Revoked run authority blocks admission/dispatch and cancels active work. The
existing finite run wall caps `not_after` and effective seconds at dispatch;
selected/effective seconds and the limiting reason remain separate evidence.
Missing run context inside a guarded execution fails closed. No new grants or
Jobs API are enabled.

`JobStore.visible` and optional synchronous `on_wait` callbacks on both public
client adapters expose execution ID, waiting/cancelling state, queued time,
observed queue ticket position/time, selected seconds, active elapsed seconds
and cleanup/recovery/unknown-outcome blocks. Ticket position is an observation,
not a round-robin completion forecast. No ETA is promised. The scheduler owns
only executable work; models, owner decisions and backoff hold no slot.

Reconnect/takeover performs the six-step sequence: fence admission, inspect
history, cancel abandoned ownership, obtain an epoch/generation-bound idle
barrier, reconcile receipts, then settle once and resume established work.
Core startup starts queue recovery without waiting for a new execution caller.
Known durable terminal receipts survive a lost reply/core commit. Missing
terminal receipts become `unknown`, never a passing result and never a replay.
Worker restart supplies a fresh epoch only after systemd cgroup cleanup.
Startup additionally requires the new daemon to be the sole member of its
unified cgroup; systemd leftovers or an unreadable cgroup block readiness.

An unknown outcome pauses its owning run and caller group, including already
queued work in that group. Unrelated groups can use the released slot. This
conservative group pause prevents legacy retry loops from manufacturing a fresh
execution ID for uncertain work. There is no automatic clearing of that pause;
it requires established reconciliation, not merely an idle worker or owner
resume. Workspace reconciliation/public APIs remain later slices. Async
cancellation and orderly controller shutdown persist cancellation intent before
closing the owned execution connection. Late success cannot erase cancellation.

## Files changed

* `bobclaw-core/core/worker/{contract,client,daemon,restrict}.py`: negotiated v2,
  bounded deadlines, identity checks, queue enforcement, supervisor protection
  and cleanup ordering.
* New `bobclaw-core/core/worker/journal.py`: durable helper, epochs, fences,
  dedup tombstones and receipt recovery.
* New `bobclaw-core/core/execution/{jobs,job_store,worker_queue}.py`: operation
  context, Postgres ledger and shared scheduler/bridge.
* `bobclaw-core/core/execution/{api,executor,store}.py`: startup/shutdown,
  propagated run ownership and migration loading.
* New `migrations/2026-11-03_worker_queue.sql`: scoped tables, keys and indexes.
* `deploy/runtime/lib/{worker,migration,lane}.py`: journal provisioning and
  retained lifecycle controls.
* `deploy/runtime/systemd/bob-worker@.service`, `bob-worker-auth@.service`, new
  `bob-worker-journal@.{service,socket}`: state/socket confinement and activation.
* `deploy/runtime/tests/acceptance.py`: separate installed protocol gate,
  controlled fault points and v2-aware cancellation/reboot probes.
* `bobclaw-core/tests/{test_worker_boundary,test_runtime_boot}.py`, new
  `bobclaw-core/tests/test_worker_protocol_v2.py`, new
  `bobclaw-gateway/tests/test_worker_queue_postgres.py`: regression, crash-window,
  real Linux subprocess and real temporary Postgres assertions.
* This document and `WORKER-BOUNDARY.md`: candidate operating/test notes.

## S43A deviations

The private journal requires a narrow separate service rather than a same-UID
daemon-owned directory. This closes the journal boundary now without pulling
Landlock or new workspace isolation into 4.3a. The Postgres session advisory lock
serves as the controller lease; generation/barrier fencing remains mandatory.
Unknown outcomes pause a whole caller group conservatively because the typed
job-step/reconciliation API is not part of this slice. The compatibility path
is the existing Python adapter, not a raw v1 execution socket.
The supervisor dumpability/signal checks close same-UID ways around the journal
and deadline boundary; they are necessary for this slice's existing bounds.

## Validation and operator acceptance

Windows venv, focused worker/boundary/runtime/execution/grant/migration suites:
**440 passed, 14 skipped**. Receipt: `.test-tmp/s43b-core-final.xml`.
The final changed-code recheck is **281 passed, 11 skipped**, recorded separately
at `.test-tmp/s43b-focused-final.xml`, including cgroup, boot-readiness and
supervisor protections. Gateway proxy/broker plus database invocation:
**18 passed, 66 skipped**;
receipt `.test-tmp/s43b-gateway.xml`. `S4B_TEST_POSTGRES_DSN` is unset here, so
**the Postgres gate is not passed**. The new storage file has 21 cases; all need
real temporary Postgres. Tests use the existing disposable local-schema fixture.

Tests used a process-only Windows `os.mkdir(0700)` to `0777` shim for the existing
sandbox ACL issue. No workspace permissions were changed. A broader Windows
attempt including existing `test_runtime_lane.py` failed on missing POSIX
`pwd`/`fcntl` imports (2 failures, 88 setup errors); those tests remain Linux
checks, not claimed passes. No Linux gate, image build, WSL acceptance, reboot
or actual 900-second execution was performed here.

Run the DB suites from `bobclaw-gateway` with core on `PYTHONPATH` and the
disposable local `S4B_TEST_POSTGRES_DSN` configured:

```sh
python -m pytest tests/test_worker_queue_postgres.py tests/test_durable_runs_v2.py \
  tests/test_agent_loop_postgres.py tests/test_grants_postgres.py
```

Build the candidate image through the existing process and complete the baseline
on two new harness-created disposable lanes. Install the updated harness at a
service-readable path (0644 file, 0755 ancestors). Then, as root:

```sh
/opt/bob/.venv/bin/python /var/tmp/bob-s43b/acceptance.py --worker-protocol \
  --lane-a s2e-test-a --lane-b s2e-test-b
```

The mode needs a live Postgres/core, unified cgroups and the installed journal
units. It performs
no provider completions. It takes at least 15 minutes: one real 900-second
timeout, with a documented 898–930 second observation tolerance, plus setup and
other probes. It covers terminal dedup/conflict, missing history/tombstones,
root-controlled admission/terminal fault windows, core intent/reply crash
windows, stale cancellation/fencing, all exit paths with detached/non-dumpable
descendants forked from a non-leader thread, journal/proc-alias denials, two run
groups plus chat/build/Git/MCP contention with root-side PID/start-time
observations, admission bounds, and active core/gateway restarts. Each exit
path admits the current 900-second bound; only the timeout path waits for it.

At the end, the mode stops lane A's core and leaves one new durable job queued
for the reboot test, without holding a worker slot. Reboot the throwaway distro
and run the existing `--after-reboot` command on the same lanes. Before the
harness can repair anything, it checks that the actual booted core recovered
that execution ID and produced its effect exactly once. It also checks retained
worker history across the real reboot. Do not start another acceptance client
between arming and reboot. Keep the baseline's host-listener positive control
and the existing separate, explicitly approved Codex-positive invocation.

Unit tests cannot prove installed mount/proc/socket isolation, systemd cgroup
cleanup, nft policy-loss handling, real elapsed timeout bounds or reboot
durability. Those are mandatory acceptance observations, not inferred from model
review, mocked state transitions or the Windows passes.

## Installed validation (2026-11-03, Windows build PC)

Built `bob-runtime:s43b2-local` from the worktree and imported it as a throwaway
WSL distro `BoB-isotest` (boundary marker present). Baseline `--create` run:
**218/218 probes passed** (the probe set varies with the host: Docker-bridge
network probes and per-PID process probes). `--worker-protocol`: **36/36 passed**,
including the real timeout (`elapsed_seconds` 900.02, `timed_out`,
`idle_proved`). After a real WSL VM restart (new kernel boot ID), `--after-reboot`
passed **10/10**, including recovery of the queued job by the booted core,
exactly once, and retained worker history. The distro was then unregistered. No
provider calls. Real Postgres suites: 372 passed, none skipped. The Linux gate
matches the 3.1 baseline with no new failures.

Defects found by the installed runs, both fixed:

1. Fresh packaged installs failed at first boot since 4.2: first boot applies
   migrations in filename order, and `2026-11-03_agent_loop.sql` sorted before
   `2026-11-03_agent_runs.sql`, which it depends on. Renamed to
   `2026-11-03_agent_runs_loop.sql`; core applies `EXECUTION_MIGRATIONS` in the
   same order, and `test_execution_migration_order.py` enforces it.
2. The harness's deliberate worker crashes plus restarts tripped systemd's
   default start-rate limit; the harness now runs `reset-failed` before each
   deliberate restart. The unit keeps the limit for real crash loops.

`wsl --terminate` alone does not change the kernel boot ID; let the WSL VM stop
(no other distro running) before `--after-reboot`.


## S43c / 4.3b candidate: job ladder and enforceable grants

This section supersedes the 4.3a-only 900-second and job-grants-unavailable
descriptions above. Candidate implementation, 2026-11-03; installed Linux
acceptance remains outstanding.

Presets are **900 / 1800 / 3600 seconds**. Service adapters attach a
JobPolicy(proposed_seconds, rationale, owner_override=None, auto_escalate=True,
custom_limit=False) to JobContext. A reviewed grant's job_override_seconds wins.
An adapter cannot independently assert an owner override. job_auto_escalate:false
enforces a strict selection. Existing short tool-adapter timeouts retain their
compatibility semantics; explicit custom job selections require grants.

The extended ladder doubles 3600 seconds (7200, 14400, ...), requiring both
allow_long_runs and allow_extended_jobs. Other durations, or selections explicitly
marked custom, additionally require allow_custom_job_limit. Selections above the
owner/installed ceiling are rejected, never silently clipped. Remaining run wall
or aggregate job time may shorten effective duration, with a persisted reason.
Exhausted resources pause without launch. The root-installed default ceiling is
86400 seconds, configurable by the operator; the finite representation bound is
2147483647 seconds. Request/output ceilings remain 1 MiB / 4 MiB. The independent
auth-publication socket retains its original 900-second v1 bound.

### Timeout, reconciliation and recovery

A known timeout with proved descendant cleanup blocks its caller group, including
already queued work. Only the continuation of that exact timeout may pass the
block. Unknown outcomes retain the existing stronger unknown-group block.
Timeout never authorizes replay or a clean workspace restart.

A trusted service adapter may pass WorkspaceReconciler(effects_check) to either
worker client adapter. It hashes actual files under the installed registered
workspace/cwd, rejects links, shared/special files and concurrent changes, and
records a bounded manifest plus adapter evidence of completed/partial work and
established effects. A model's assertion or daemon idle receipt cannot establish
effects or resumability. Evidence is bounded to 1 MiB; scanning is bounded to
10000 entries / 256 MiB read. Exceeding bounds pauses rather than omitting files.

Core independently verifies the current hash at continuation admission and again
at dispatch. A new immutable execution ID records the parent, original logical
job ID, timeout episode, next selection, pinned grant and evidence. Concurrent
continuations recover one child through a unique parent index. Changed state,
uncertain effects, unsupported adapters, revoked authority, strict limits and
exhausted resources pause and flag. A run-wall/aggregate-time truncation cannot
grow through the ladder. Recovery rechecks authority, cancels abandoned work,
imports known receipts once and preserves full unknown-time holds.

This supplies the internal continuation seam for 4.3d. Generic shell/build tools
do not acquire a fabricated effects checker: without one they pause visibly.
Workspace isolation, Landlock, public Jobs API, editing, apply and CLI enrollment
remain later slices. Coverage matches registered workspace/cwd requests; it does
not add filesystem confinement.

### Grants and accounting

Matcher v3 adds explicit job_coverage to the existing owner grant service. V1
exact-task and v2 project semantics/permissions remain unchanged. V3 project jobs
must remain subsets of the reviewed profile revisions, workspace relative paths,
attachment IDs, CLI eligibility and job budgets, within the original actor, team
and model boundary. No replacement grant is silently chosen for a pinned grant.

Admission, dispatch, continuation and recovery share the existing grant fence.
Revocation takes it exclusively, flags runs and marks queued/active jobs for
cancellation. cleanup_pending stays true until cleanup is proved. Owner-session
expiry does not cancel a grant; grant expiry/revocation does. CLI coverage remains
unavailable, independently of the grant schema.

Every durable root gets finite max_job_attempts (default 32) and optional
job_seconds_budget. Dispatch reserves one attempt and effective seconds before
IO. Known terminal receipts settle measured execution/cleanup time once.
Missing measurements conservatively consume the reservation. Unsent cancellation
charges nothing; unknown execution keeps its full hold. Migration preserves
historical consumption conservatively. Exact model-call accounting is unchanged;
CLI internal usage is explicitly estimated/unknown.

### Exact app contract additions

Changes are additive within execution contract v2. Negotiate the live v2 catalog
or GET /capabilities?operations=1&contract_version=2.

- capabilities_revision is execution/2.4 (execution/2.5 once 4.4 failure recovery and flags are combined); grant_matcher_versions is [1,2,3].
- job_time contains version:1, presets_seconds:[900,1800,3600],
  agent_proposes:true, owner_override:"reviewed_grant", strict_limit:true,
  finite_custom:"matching_owner_grant",
  above_3600:"allow_long_runs_and_allow_extended_jobs",
  custom:"allow_custom_job_limit", matcher_version:3,
  continuation:"trusted_adapter_reconciliation_required",
  max_job_attempts_default:32, job_seconds_budget:true,
  cli_usage:"estimated_or_unknown", codex_cli:false, jobs_api:false.
  Live discovery adds available, reason, installed_max_seconds and
  profile_revisions. Old/offline workers report unavailable.
- Owner operation descriptors add job_grants_available, job_grants_reason and
  job_grant_matcher_version:3. General grant availability and job-grant
  availability are distinct. The owner API requires negotiated installed ladder
  support before issuing enabled job permissions.
- runs.start.budget and prepared requests accept optional positive
  max_job_attempts and nullable positive job_seconds_budget.
- Prepared/start requests accept optional job_coverage:
  {profiles:{profile:revision}, workspaces:{registered_id:[relative_path]},
   attachments:[id], max_job_attempts, job_seconds_budget, allow_cli:false}.
  Paths are relative without traversal. Requested budgets cannot exceed coverage.
  Enabled CLI coverage is rejected. The immutable proposal reports matcher 3.
- grants.issue enables existing allow_extended_jobs, allow_custom_job_limit and
  nullable job_seconds_ceiling; optional job_override_seconds and
  job_auto_escalate are new. Job permissions/overrides require explicit coverage
  in the proposal. Grant get/list/issue return these fields, job_coverage and
  matcher_version. Existing owner step-up/revision/request-key rules remain.
- runs.get.run.job_account contains max_job_attempts, job_seconds_budget,
  dispatched, held, completed, unknown, seconds_used, seconds_held,
  cleanup_seconds and cli_usage_basis, independently of exact calls.
- Internal wait projections add proposal/rationale/override, selected/effective
  limits and reason, logical/parent execution IDs, timeout episode, reconciliation,
  pinned grant and timeout blocking. Returned timeout receipts include
  continuation_requires_reconciliation:true.
- Positive integer fields use the existing safe-number-or-decimal-string encoding.
  worker_jobs remains false: no public Jobs routes or typed job-loop topology are
  advertised. Only the backend broker was edited in this worktree.

### Files and design choices

New core/execution/job_policy.py and job_reconciliation.py implement policy and
actual workspace evidence. jobs.py, job_store.py and worker_queue.py persist and
enforce them. Execution models/grants/store/owner/api add reviewed coverage,
accounts and live support. Worker contract/client/daemon and runtime lib/worker.py
enforce the installed ceiling. The ordered migration is
2026-11-03_worker_time_ladder.sql, included in EXECUTION_MIGRATIONS.
Gateway capability/owner routes and backend broker expose the additive contract.
Unit/Postgres tests and installed acceptance cover the new behavior.

S43A deviations: existing short tool timeouts retain compatibility; explicit
JobPolicy distinguishes the new tier/custom selection. Generic effectful adapters
pause until trusted checks establish resumability. Manifest scanning is bounded
and conservative. Full workspace isolation/public mutation routes remain in 4.3c.
The existing attempt ledger, grant fence and scheduler are reused; no parallel
approval flag, scheduler or model-call budget was added.

### Validation and operator commands

Receipts are under .test-tmp/ in this worktree. The Python 3.13 Windows mkdir(0700)
ACL issue required a local pytest runner preserving inherited temp-directory
ACLs; application permissions were unchanged. The broad compatible core pass was
431 passed / 11 skipped. Later focused receipts cover subsequent worker and
reconciliation changes. A separate runtime-lane invocation failed on Windows
because pwd/fcntl are absent, and is not a Linux-gate pass. The final gateway/owner/broker
pass is 49 passed / 98 skipped (.test-tmp/s43c-last-gateway.xml).
The final focused policy/worker/grant/migration pass is 189 passed / 10 skipped
(.test-tmp/s43c-last-core.xml); the preceding broader worker recheck was
242 passed / 10 skipped (.test-tmp/s43c-focused-check.xml).
Postgres is not configured here. Skipped DB/Linux tests are not passed.

No image build, deployment, external network/provider call or installed elapsed-time
acceptance was run. The sibling runs/gate-s43b baseline is unchanged.

On Linux, set S4B_TEST_POSTGRES_DSN to disposable local Postgres and run existing
execution, loop, grant and worker suites plus tests/test_job_ladder_postgres.py
(gateway) and tests/test_job_time_ladder.py (core). The image allowlist already
includes the new modules, broker and migrations; the existing explicit
BOB_RUNTIME_WORKTREE=1 candidate-build path includes uncommitted changes.

After building/installing into marked disposable isotest lanes, run separately as
root with the installed harness at its public script path:

~~~sh
python3 /var/tmp/bob-s2d/acceptance.py --job-ladder --job-tier 900
python3 /var/tmp/bob-s2d/acceptance.py --job-ladder --job-tier 1800
python3 /var/tmp/bob-s2d/acceptance.py --job-ladder --job-tier 3600
~~~

Add --lane-a NAME --lane-b NAME for nondefault disposable names. Repeated
--job-tier flags combine tiers; omission defaults to 900. Each invocation runs
actual grant denial/authorization, revocation/expiry, accounting and remaining-wall
checks, followed by the selected full elapsed-time preset(s).

At each preset root observes four detached/non-dumpable double forks from
non-leader threads, PID/start-time identity, predeadline liveness, late fork races,
stopped heartbeat writes and the cleaned whole worker cgroup. Timing tolerance is
-2/+30 seconds. JSONL receipts include IDs, kernel/profile/daemon hashes, elapsed
observations and cgroup members; failure exits nonzero. Retain --worker-protocol,
baseline and --after-reboot checks. --codex-positive stays separately approved.
