# S4f / 4.4 failure recovery and owner flags

Candidate backend implementation, 2026-11-03, `wip/sc` based on `cbfe18f`.
No commits, deployment, external service operations, or real provider calls.
The sibling `runs/gate-s43b` baseline is unchanged.

## Behavior

The v2 executor now owns a durable failure episode instead of completing a
failed check and asking the planner to try again. Initial failure allows two
same-binding retries, then an approved different-model audit, then one guided
call on the original binding. Output must pass the server's deterministic
acceptance checks before publication. Audit prose never counts as a check.

Backoff defaults to 2/8 seconds. Numeric and HTTP-date Retry-After values are
bounded to 60 seconds. Postgres stores the absolute retry time. Counts advance
atomically with the existing charge transaction, not when a timer starts or an
unsent reservation is created. Restart releases unsent reservations without
resetting dispatched counts. A dispatched audit/guided call lost at restart is
unknown and pauses; it never gets another free attempt. Recovery cannot grow its
budget. Admission, grants, cancellation, deadline and pool checks still apply
before reservation and charge, including after backoff.
Restart at an exhausted recovery step also pauses instead of entering ordinary
loop auto-growth. A separately authorized owner budget/wall resume restores only
the saved pending stage and unused attempts; it preserves the episode and all
counts. Unknown effects and exhausted ladders remain non-resumable.

Current owner policy applies to every initial, retry, audit and guided call at
reservation and charge. Policy narrowing pauses all active failure episodes,
even when a previous flag remains open. Safe unsent recovery retains its pending
stage and counters; widening alone never resumes it. Explicit owner Resume must
pass the current policy and all existing checks. Dispatched unknown calls remain
charged and non-resumable. Sessions without stored operation lists retain their
original 3.1 permissions and do not gain settings or flag-resolution authority.

Team definitions may include `audit_bindings: [binding_id, ...]`, in approved
order. Admission snapshots those catalog bindings along with the role bindings;
grant matching therefore covers the audit authority too. No implicit critic,
roster, team, provider or paid fallback is inserted. Selection intersects the
snapshot with current policy, exact text-transport eligibility, model identity
and backend readiness. Different faces sharing a model are excluded. The
production dispatcher uses existing non-generating health clients, failing
closed on probe exceptions/timeouts. These checks are the backend's readiness
evidence, not a guarantee that inference will succeed. Once chosen, the auditor
is pinned across its own bounded transient retries. Malformed audit JSON never
starts another audit or a schema-correction ladder.

Audits have an exact versioned schema, bounded raw/parsed output, actual
server-stamped reviewer identity, current-run attempt/evidence references and
server-registered required checks. The only registered check in these text
topologies is `acceptance`, which reruns the existing deterministic validators.
The auditor can advise pause or bounded correction; it cannot change scope,
model authority, budgets, effects, tools or checks. Parsed audit and raw digest
are also emitted into the paginated run event trail, including successful
recoveries.

Fingerprints use server acceptance identity/phase/base/error rather than model
task wording, task IDs or planner cycles. Equivalent replanned failures share
counts; this is deliberately conservative for multiple tasks targeting the
same criteria. A partial unique index also prevents concurrent in-flight
recovery calls for the same episode. An exhausted/paused episode is not reset
by flag resolution or owner budget resume.

Current enabled operations are supplied-evidence text operations and retain a
server-defined `read_only` class. The reconciliation policy also handles
`idempotent_by_key`, `workspace_mutation`, and `external_or_unknown`, without
enabling those adapters. A repeatable non-read-only operation needs a verified
server receipt, plus the original request key or matching workspace base/output
hashes where applicable. Applied or unresolved effects pause. Audit output
cannot supply that receipt. No shell/test job is newly classified read-only.

Existing worker `worker_outcome_unknown` transitions create a durable flag in
the same DB transaction through a narrow projection trigger. Migration also
projects pre-existing paused unknowns once. It neither clears the caller-group
pause nor changes worker recovery/replay. This avoids modifying sibling streams'
worker/job/grant implementation files. Other execution pauses use the same
scoped flag service. A pause settles concurrent dispatched siblings as unknown;
cancellation still wins over late success.

## Exact app/broker contract

JSONL remains `{id,op,args}` / `{id,ok,value|error}`. These operations require
`contract_version:2`; capability discovery advertises `flags.list`, `flags.get`
and owner-session `flags.resolve`. An older server fails with
`client_upgrade_required`; no reduced or legacy resend occurs.

| Operation | Args besides contract_version | Gateway route / result |
|---|---|---|
| `flags.list` | `run_id?`, `status?` (`open` default, `resolved`, `all`), `cursor?`, `limit?` (1–50) | `GET /agent-services/flags`; `{items,next_cursor,source:"server"}` |
| `flags.get` | `flag_id`, `after_event_seq?`, `job_cursor?` | `GET /agent-services/flags/{flag_id}`; `{flag,run,events,next_cursor,next_job_cursor}` |
| `flags.resolve` | `flag_id`, `expected_revision`, `request_key`, `decision`, `guidance` | `POST /agent-services/flags/{flag_id}/resolve`; `{flag_id,run_id,resolved:true,run_resumed:false,operation_id}` |

`flags.resolve` decisions are `inspected`, `resume_with_guidance`,
`authorize_budget`, or `cancel`. Guidance is required and bounded to 4000
characters. Request keys and positive revisions use the existing v2 integer
encoding and idempotency rules. Repeating the same key/payload returns the same
receipt; changed payload or stale revision conflicts. Foreign IDs return 404.
Resolution records the owner's decision; it does not dispatch, reconcile an
unknown effect, issue a grant, cancel a job, reset retry counts, or resume a run.
The app must invoke the existing separately authorized action and inspect its
receipt. Existing budget/wall resumes retain their existing eligibility rules;
an exhausted failure ladder cannot be restarted by acknowledging its flag.

Read operations use the ordinary portal/agent transport. Agents must specify
their currently authorized run for list, and list/get enforce its initiating
actor/scope boundary. Project/conversation ownership is revalidated separately.
Resolution uses only the separate 3.1 owner session, its audience, operation
allowlist, credential epoch, portal credential and broker link. Plain human,
portal and agent tokens cannot resolve. The owner review challenge reads the
actual flag through its vouched server read, not a client-written summary.

Flag summaries contain stable IDs, episode/run/step, stopped phase, typed cause,
revision, status and timestamps. Detail includes project/conversation linkage,
original scope/team/binding policy, stopped budget snapshot, current requested /
effective / authorized budget, used/unknown/reserved counts, usage provenance,
attempt trail, auditor input/raw/parsed result, effects, output/check links,
live job cleanup and safe action metadata. Events page with `after_event_seq`
and `next_cursor`; job cleanup pages with `job_cursor` and `next_job_cursor`.
Usage remains estimated/not billing-accurate. Model critique does not set
`independently_verified`.

Pause-created flag snapshots keep model-call and worker-job accounting separate.
In their stopped context, `account` and `reserved_calls` are null when no call account/allocation
exists; flagging never creates one. `job_account` records any existing job counts
and time holds, and is also included in the current flag `budget`. Revocation and
policy narrowing must commit their pause, cancellation intent and flag even for
worker-owned runs without call accounting. Flag creation only reads these
accounts: it does not settle job holds or take queue/account locks after run
locks. The existing controller session -> grant/policy fence -> queue -> rows
order and the queue's receipt-based cleanup/settlement remain unchanged.

Run get/list add `flags` summaries, `failure_recovery` summaries (including
resolved episodes), and `failure_recovery_truncated`; the summary limit is 50.
The full event trail remains paginated. Relevant events are `failure_recorded`,
`second_eye_audit`, `failure_recovered`, `flag_raised`, `flag_resolved`, plus
existing pause/check/cancel events. The app should deduplicate badges by flag ID,
not by reconnect/event delivery. No notification messages/email are sent.

The app broker patch is at `runs/S4F-APP-BROKER.patch`, outside this
repository, against app commit `bb2f6dd` with paths
`a/portal/portal-broker.py` and `b/portal/portal-broker.py`. Backend tests do not
apply it. The app-copy fixture skips when the sibling broker lacks owner-session,
settings/policy or flags operations; source and packaged fixtures remain required.

## Files and rationale

Paths below are relative to this backend worktree.

| Files | Purpose |
|---|---|
| `bobclaw-core/core/execution/failure.py` (new) | Failure/effect policy, bounded backoff, strict audit validation, healthy approved selection and resumable controller. |
| `bobclaw-core/core/execution/failure_store.py` (new) | Transactional episodes, attempts/audits, flag read/resolve and reconciliation guards. |
| `migrations/2026-11-03_z_failure_recovery.sql` (new) | Scoped durable records/FKs, one in-flight recovery per episode, worker-unknown flag projection/backfill. |
| `bobclaw-core/core/execution/{executor,store,loop_store,budgets}.py` | Connect failure control to both v2 topologies, existing fencing/reservation/charge/checkpoint paths, cancellation settlement and app projection. Migration registered in filename order. |
| `bobclaw-core/core/execution/{models,admission}.py` | Approved audit order validation, immutable audit pins, capabilities and independent live audit eligibility. |
| `bobclaw-core/core/execution/{api,owner}.py`, `bobclaw-core/core/permissions.py` | Vouched flag commands, resource checks, 3.1 owner operation and signed-operation allowlist. |
| `bobclaw-core/core/execution/dispatcher.py` | Preserve HTTP Retry-After/native failure types, non-generating fail-closed auditor readiness. |
| `bobclaw-core/core/backends/{openai_codex,openai_codex_auth}.py` | Add optional sanitized failure metadata so timeout/malformed/HTTP failures remain distinguishable. Legacy error messages, transports, auth and retry behavior are preserved. |
| `bobclaw-gateway/{auth,owner_sessions}.py`, `bobclaw-gateway/routers/{execution,agent_services,capabilities}.py` | Exact flag routes, scoped reads, owner challenge/session enforcement and negotiation. |
| `portal-broker/portal-broker.py`, external `runs/S4F-APP-BROKER.patch` | Exact JSONL forwarding and separate owner transport in backend/package and app patch. |
| `bobclaw-core/tests/test_failure_policy.py` (new) | Classification, schema/evidence/identity attacks, effect receipts, native metadata and health selection. |
| `bobclaw-gateway/tests/test_failure_recovery_postgres.py` (new) | Real temporary-Postgres acceptance: retries/audits/checks, crash windows, counters/backoff, control races, effect safety, worker projection, scopes and deduplication. |
| `bobclaw-gateway/tests/test_failure_flag_contract.py` (new) | Real gateway/vouch/3.1 sessions and source/package/app-copy broker behavior. |
| `bobclaw-core/tests/{test_agent_loop,test_durable_execution}.py`, `bobclaw-gateway/tests/{test_agent_loop_postgres,test_execution_broker_v2}.py` | Adapt executor seams to common reservation API; replace old unchecked replanning assertions with 4.4 policy; verify actual shipping snapshot and compatible sibling app copy. |
| This document | Implementation, deviations, validation and app handoff. |

No worker, job, queue or grant implementation file was edited. Legacy v1
gateway execution remains on its original path. No roster/provider registration
or runtime helper/settings change was made.

## 3.3 credential-generation integration

The policy-narrowing recovery test previously set `BOB_SETTINGS_ID` to
`fixture-settings` after the shared harness issued its human JWT. Since 3.3,
that variable also enables installed credential enforcement: a valid settings
ID, loaded epoch and root-published active epoch are required. The HTTP 503 at
`/teams` was the generation guard rejecting this incomplete fixture, before
any recovery code ran. Merely stubbing that guard would leave an epoch-less JWT
and durable authority and would miss the owner-session boundary.

`installed_recovery_h` supplies the installed generation and owner mapping,
reissues the human JWT, initializes real disposable SQLite owner-session state,
and leaves credential and dispatch guards enabled. Only the root-published
pointer and model transports are synthetic; run/recovery state remains in real
temporary Postgres. Resume now goes through the actual gateway/core route with
a password/TOTP owner session instead of calling Store.resume directly.

All six original basic/audit/guided and unsent/dispatched combinations remain.
Six additional epoch-change cases check gateway mismatch, stale owner-session
rejection after reload, and a fresh session's inability to revive old-epoch
run authority. Policy widening alone never resumes; same-epoch safe recovery
retains its stage/backoff/counters; dispatched unknown calls remain charged and
non-resumable. No production epoch, session, grant, worker or recovery guard was
relaxed. Settings and job-ladder tests that call policy storage directly do not
traverse the gateway generation guard; HTTP fixtures enabling installed settings
must supply the same credential lifecycle.

This reconciliation was reviewed statically. Postgres is unavailable here and
local process startup was denied, so the updated tests have not been executed.
The historical counts below do not validate these additions.

## S4A refinements and validation

The failure fingerprint uses stable acceptance identity rather than model task
IDs, so replanning cannot manufacture a new retry budget. The worker flag
projection is a trigger because the 4.3a queue writes that state directly;
changing its implementation here would conflict with the parallel job stream.
Flag resolution records a decision, as specified; subsequent actions remain
separate existing owner commands. The app patch is delivered separately at
`runs/S4F-APP-BROKER.patch`, outside this repository. No new privileged helper or
effect adapter is enabled.

Windows tests use the existing `.venv-win` interpreter. A process-only mkdir
shim maps pytest's Windows 0700 temp-directory creation to 0777 for this sandbox;
no existing directory permissions or repository controls were changed. Receipts
are `.test-tmp/s4f-core.xml` and `.test-tmp/s4f-gateway.xml`.

Original pre-rebase Windows results (historical; included patched-app tests):
**406 passed, 10 skipped** in core; **48 passed, 97
skipped** in gateway/broker/storage invocation. The ten core skips require Linux
POSIX/seccomp/subreaper behavior. Gateway skips comprise **95 real-Postgres
cases**, one POSIX collection, and one unpatched-app flag case. Source, packaged,
and patched-`bb2f6dd` flag contracts passed. The existing unpatched app copy is
not claimed to implement flags. `git diff --check`, LF checks, and applying the
app patch against the isolated exact baseline passed.

Rebase reconciliation on Windows, 2026-11-04: **411 passed, 10 skipped** in
core; **79 passed, 121 skipped** in gateway/broker/settings/execution; **57
passed, 16 skipped** in runtime migration/helper tests. Gateway skips include
108 real-Postgres cases, 12 sibling-app cases predating settings/policy, and one
POSIX-only collection. Receipts are `.test-tmp/rebase-core-final.xml`,
`.test-tmp/rebase-gateway-final.xml`, and `.test-tmp/rebase-runtime.xml`.
Added real-Postgres cases cover policy narrowing at basic/audit/guided stages,
safe explicit resume, and refusal to replay dispatched unknown calls; those
cases remain skipped without a disposable DSN. An expanded gateway auth suite
with isolated test state and fail-closed POSIX-locking import shim recorded
**184 passed, 2 failed**: the two token-file ownership tests require `os.getuid`,
which Windows lacks (`.test-tmp/rebase-auth-isolated.xml`). These results do not
verify Linux ownership, locking, helper-peer or worker containment enforcement.

The real Postgres gate requires a disposable local `S4B_TEST_POSTGRES_DSN`.
Without it the DB tests skip; this does not pass persistence, SQL, migration,
crash or concurrency acceptance. No SQLite/in-memory replacement was used for
those assertions. Linux isolation/reboot acceptance is still the operator's
gate, and its baseline receipts were not rerun or changed here.

With that DSN set, run from `bobclaw-gateway` with core on PYTHONPATH:

```sh
PYTHONPATH=../bobclaw-core python -m pytest tests/test_failure_recovery_postgres.py \
  tests/test_durable_runs_v2.py tests/test_agent_loop_postgres.py \
  tests/test_grants_postgres.py tests/test_worker_queue_postgres.py
```

Apply the external `runs/S4F-APP-BROKER.patch` in the app stream separately.
Review and run the existing Linux gate before enabling this candidate.
