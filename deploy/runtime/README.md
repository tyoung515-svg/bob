# Bob runtime image (R2)

This directory builds an Ubuntu 24.04 amd64 root filesystem for Docker smoke
testing and WSL2 import. It does not install or start anything on the build host.
The CPU llama.cpp server and its shared libraries come from the pinned upstream
Ubuntu CPU release. Qdrant is the upstream musl static binary. There is no GPU
integration or extractor service.

## PA2 embedder and first-boot acceptance

The packaged `llama-server` and all its libraries share `/opt/llama`;
`/usr/local/bin/llama-server` is a symlink. b11747 resolves the executable through
`/proc/self/exe` and scores the available CPU variants in that directory. Keep
`GGML_BACKEND_PATH` unset: it names one out-of-tree library, not a directory.
`GGML_BACKEND_DIR` in that release is a compile-time search path, not an env fix.
The installed `/usr/local/libexec/bob/check-embedder-backend` runs the packaged
binary from `/` with `--verbose` against an absent model and requires the
`load_backend: loaded CPU backend from /opt/llama/libggml-cpu-*.so` receipt followed
by the model-file error (so discovery finished). `--list-devices` is not used: in
b11747 it lists no CPU device and exits 0 even with no backend loaded. The image build runs this same check. `kmod` supplies
`/usr/bin/kmod` for the distro's `kmod-static-nodes.service`; no units are masked.

`bob-health` preserves its existing keys and exit convention, including
`healthy` (boot/lane health) and `lanes` (object) used by the app. It adds
`failed_units` and `embedder` with `status`, `ready`, `setup_needed`, `model_path`
and `detail` (plus `health` when the model exists). A skipped, model-free
embedder reports `status="setup-needed"`, `ready=false`, `setup_needed=true`,
and `detail="embedder model not installed at /var/lib/bob/models/Qwen3-Embedding-4B-Q4_K_M.gguf"`.
Boot/lane health can still be true in that setup state. While llama-server is
loading an installed model the status is `starting`. A failed or stopped unit, or a
failed unit query, reports embedder failure. `failed_units` lists every failed
system unit for diagnosis, but only Bob's own units gate `healthy`, so an unrelated
failed unit in a user's distro cannot block setup. The acceptance `--embedder`
mode separately requires `systemctl --failed` to be empty on a fresh image.
The app's current parser accepts the additional fields but does not display them.

Run the offline suites from the repository root:

```bash
python -m pytest deploy/runtime/tests -q
```

Build the uncommitted candidate with `BOB_RUNTIME_WORKTREE=1 bash
deploy/runtime/build.sh pa2-runtime-20261104` (the default build uses committed
HEAD). Import the resulting archive into a **new disposable WSL distro** using
the usual runtime import procedure. Tests are excluded from images: copy both
`tests/acceptance.py` and `tests/embedder_acceptance.py` into
`/var/tmp/bob-pa2`, directories 0755 and files 0644, before the following checks.
As root inside that runtime, after creating a test lane named `pa2-test`:

```bash
/opt/bob/.venv/bin/python -I /var/tmp/bob-pa2/acceptance.py --embedder --lane-a pa2-test
```

This observes boot state and `systemctl --failed` **before** any service repair,
requires no failed units (no allowlist), checks the CPU load and real model-free
`bob-health` receipt, then explicitly skips the vector probe if
`BOB_ACCEPT_EMBED_MODEL` is unset. No model is downloaded, installed, or moved by
the harness. Run this pass before installing any model.

For the second pass, manually install an existing approved GGUF at the exact
packaged model path, owned by `bob-embedder:bob-embedder`, mode 0600. Start
`bob-embedder.service` and wait for `curl -fsS http://127.0.0.1:58081/health`
to return `{"status":"ok"}`. Then run:

```bash
BOB_ACCEPT_EMBED_MODEL=/var/lib/bob/models/Qwen3-Embedding-4B-Q4_K_M.gguf \
  /opt/bob/.venv/bin/python -I /var/tmp/bob-pa2/acceptance.py --embedder --lane-a pa2-test
```

The supplied path must identify the already installed model; alternate files
are rejected. This checks GET `/health` and runs the real
`core.memory.embedder.SlotResolvedEmbedder` via `runuser` as that lane core's
service identity, cross-checked against systemd and the provisioned UID. The
child loads the lane's `core.env` and configured slot file, rejects nonpackaged
endpoints before HTTP, and requires two distinct, finite, nonzero 2560-element
vectors. No memories or provider calls are created. JSONL ends in `overall`,
and failure exits nonzero. The missing-model probe skips in this second pass;
retain both passes as evidence. Re-run after an actual distro reboot to check
first-boot versus subsequent-boot behavior.

## Build and export

`build.sh` uses **only `git archive HEAD`**. Local edits, untracked files, `.git`,
host virtualenvs and live secrets are not build inputs. The sole tracked secret
template is `.secrets/bobclaw.env.example`. Review the tracked tree before building.
The R2 implementation must be in HEAD before this script can build it; implementation
of this brief does not make a commit.

Replace the four digest placeholders with independently verified SHA256 values.
`R2-PINS.txt` supplies the Qdrant, llama CPU archive and GGUF digests and URLs,
and pins uv to 0.13.6; it does not supply a uv archive digest. `LLAMA_SHA256` is
the **CPU binary tarball** digest. `EMBEDDER_URL` matches the supplied HF URL:
it uses `main` and is content-pinned by the digest, rather than an immutable HF
commit URL. If main changes, fetch fails on mismatch. Supply a commit URL with
`--build-arg EMBEDDER_URL=...` if URL immutability is also needed. These supplied
pins were not network-verified here; release availability, archive dependencies
and model compatibility require the operator's build and smoke test.

```bash
bash deploy/runtime/build.sh r2 \
  --build-arg UV_SHA256=<uv-0.13.6-linux-gnu-tarball-sha256> \
  --build-arg QDRANT_SHA256=<qdrant-1.18.0-linux-musl-tarball-sha256> \
  --build-arg LLAMA_SHA256=<llama-b11747-cpu-binary-tarball-sha256> \
  --build-arg EMBEDDER_SHA256=<gguf-sha256>
bash deploy/runtime/smoke.sh bob-runtime:r2
```

Unfilled/malformed download digests fail before download; mismatches fail the
build (or model fetch) and delete the downloaded file. The GGUF is fetched only
by `bob-embedder fetch`, not during the image build. Python is the uv-managed
3.13 line; package resolution follows the two existing requirements files.

The export is `dist/bob-runtime-r2.tar.gz` and its adjacent `.sha256`. It comes
from `docker export` of a **created, never booted** container, so it contains no
firstboot secrets, initialized database or test lane. No Docker volumes hide
the state directories from the export. Existing export files are not overwritten.

The smoke script boots `/sbin/init`, waits up to 240 seconds for firstboot, adds
an `owner` lane with an ephemeral random password, and waits up to another 240
seconds (plus command execution time) for health. Its EXIT trap stops/removes
the test container. Its default has no `--privileged` flag or host cgroup bind.
Some Docker hosts cannot boot systemd under those constraints. On such a host,
inspect the printed logs and explicitly opt in with `BOB_SMOKE_PRIVILEGED=1` if
desired. The script never retries with privilege automatically.

## First boot and lanes

Run runtime administration commands as **root inside the runtime** (for WSL,
`wsl -d <distro> -u root -- ...`). The default interactive user is the system
account `bob`; the image does not grant it passwordless sudo. Source and the
virtualenv live under `/opt/bob`; writable application state lives under
`/var/lib/bob` (bob-owned, 0700). Postgres and Redis keep their distro-managed
state in `/var/lib/postgresql` and `/var/lib/redis`.

`bob-firstboot.service` uses `deploy/new-env.py`, normalizes database/Redis URLs
to IPv4 loopback, sets the shared Postgres role password, creates `bobclaw`, then
applies `init.sql` followed by sorted `migrations/*.sql`. The
`bob_runtime_migrations` table records filename, content digest and timestamp in
the same transaction as each file. Interrupted setup can rerun and skips
completed files; a changed applied file fails. The final
`/var/lib/bob/.firstboot-complete` marker prevents subsequent boot-time work.
For an intentional schema replay check, stop lanes, remove only that marker,
then restart the oneshot; do not delete the migration table or shared secrets.

```bash
bob-lane add owner                    # hidden password prompt
bob-lane add owner --password-stdin    # one password on stdin, optional final LF
bob-lane list
bob-lane rotate-token owner           # replace the lane's portal broker bearer
bob-lane remove owner --keep-data
bob-health
bob-embedder fetch
```

Names match `[a-z][a-z0-9-]{0,31}`. Allocation is locked and chooses the first
free pair starting at 7835/7836, then 7837/7838, checking configured reservations
and currently bound loopback ports. Bridge ports start at 8902 and avoid all
reserved/occupied ports. The three instance units load shared secrets first and
the lane's 0600 `lane.env` second. Each lane receives a fresh TOTP secret and
JWT/core-vouch secret, separate memory/gateway SQLite files, Qdrant collection
prefix, federation registry, project/scratch/config directories and sidecar.
The relative core cache is isolated by the lane-specific working directory.
The shared Postgres database/role and Redis remain shared, as specified; these
process lanes are not an OS security boundary (all run as bob).

Lane creation also mints `/var/lib/bob-portal/<lane>.token` using the
gateway's `create_portal_token` in a fresh process with shared secrets loaded
before `lane.env`. Its scope allows the lane and `shared`, and explicitly denies
every other configured lane (including retained lanes) at mint time. Portal
tokens allow exactly the broker's REST routes in strict mode, plus owner chat
and approval WebSockets; face selection is unrestricted within the lane.
The sibling `<lane>.json` contains only `{"gateway_port": N}`. Both files are
atomically replaced, 0600 and owned by bobapp:bobapp inside the 0700,
bobapp:bobapp `/var/lib/bob-portal` directory. The system user bobapp has no
login shell or home directory; bob is not a member of its group, so core and
its bob-run workers cannot read the portal files.
`rotate-token` does not revoke previously issued
JWTs, which retain their normal expiry. Neither command prints the bearer.
`PORTAL_TOKEN_DAYS` defaults to 30 independently of the agent TTL.
The root `bob-token-renew.timer` runs daily and invokes
`bob-lane renew-if-due <lane>` for each active lane. A token with fewer than
seven days left is replaced. Rotation and renewal migrate old lane-directory
`bob-memory/broker.token` files to the bobapp-only paths and remove the old file,
including when a fresh portal token already exists. Old agent tokens are also
replaced with portal tokens.
The broker reloads the file for each REST call and WebSocket upgrade.

## Lane model setup

Run `bob-model` as root inside the runtime (the app invokes it via `wsl.exe`).
`bob-model status <lane>` prints one JSON object with credential-presence flags,
the configured default and readiness. Its `chatgpt` object reports `signed_in`,
`selected_model`, `last_refresh` (UTC or null), and `model_selection_required`.
It inspects only that lane's files; signed in means stored sign-in is present,
not that the provider has accepted a request today.

`bob-model openai-login <lane>` runs Codex device login as the lane worker in
worker mode (as bob in legacy mode), streams the device URL/code as a JSON line,
and finishes with a done/error event. The device-login subprocess limit is 15
minutes; the post-login model read is capped at 20 seconds. Raw Codex output is withheld. After successful sign-in it reads the visible account model
list. If no HTTP model is configured, it saves the account default (explicit
default first, then priority and slug). Existing model overrides and lane defaults
are preserved. With no lane default, sign-in chooses `openai_codex`, the GPT chat
transport with Bob's tools. If the model list is unavailable, sign-in still succeeds;
the done event's `chatgpt.model_notice` and catalog say to choose a model.

`bob-model openai-models <lane>` returns JSON with `ok`, `lane`, `backend`,
sign-in state, `selected_model`, `last_refresh`, `model_selection_required`,
`account_default`, and `models: [{slug, account_default, selected}]`.
This is a read-only account GET: no generation, sign-in refresh, lane writes or
restart. An expired session may require another sign-in. The account default is
the first visible model under the same rule used by inference.

`bob-model openai-set-model <lane> <slug>` re-reads that list, refuses slugs not
visible to this account, saves `OPENAI_CODEX_MODEL` plus a bounded catalog snapshot,
regenerates the role environments, and restarts core (the gateway follows).
It returns the same JSON shape with the new selection. It preserves the lane's
default backend; use `bob-model set-default <lane> openai_codex` to switch ordinary
agent turns to GPT. Up to 12 visible models are published for conversation pins
and workflow bindings, account default first and selected model retained. List
returns every visible slug; choosing one outside the snapshot enrolls it. Later
account-list changes are published by sign-in or model selection, not by list.
There is no synthetic Sol alias: `gpt-6.1-sol` is selectable only if this account
actually lists it. Old signed-in lanes can run list, then set-model without a new
login. A missing model is labelled “GPT (ChatGPT sign-in): choose a model”.
Expected selection failures return `ok=false` plus a sanitized `error: {code, detail}`
and exit 1; success exits 0. No command prints credentials or account identifiers.

`bob-model set-key <lane> <provider>` reads the key from stdin. Providers are
`openai`, `anthropic`, `deepseek`, `google`, `glm`, `kimi`, `qwen`.
OpenAI keys still go through Codex key login and enable only the Codex coding
agent, `codex_code`. They do not enable the GPT tool transport. Settings deliberately
omits OpenAI from its vault-key list. For the other six providers, installed lanes
use the root settings vault/journal; legacy lanes without a settings identity use
0600 `providers.env`. The first successful setup chooses the provider preset:
`codex_code`, `claude_api`, `deepseek_v4_flash`, `gemini_flash`, `glm_5_3`,
`kimi_code`, or `qwen_cloud`, respectively. Later setup preserves an existing
default. `bob-model set-default <lane> <backend>` validates enrollment, changes the
default and restarts core; it accepts core backend names plus alias `claude`.
Choosing a default does not provision credentials.

Each lane has its own `bob-memory/codex-home` and native OpenAI `gpt` CLI profile
without an invented model pin. Existing owner profiles survive provisioning.
Worker ownership and shared-auth rules are in [WORKER-BOUNDARY.md](WORKER-BOUNDARY.md).
`CODEX_HOME` is recorded in `lane.env`. The image installs checksum-pinned Codex
0.166.0. Configured-lane face routing uses the selected, enrolled lane default;
missing setup is reported rather than substituting another provider. The public
setup name `claude` maps to core's `claude_api` dispatch. `codex_code` remains the
Codex coding agent, without Bob's native tool loop or a workflow binding.

Validation limitation: **real sign-in completion not yet run**. Account visibility,
provider-reported model names, and live Assistant/workflow tool turns still need
the owner's runtime check. No unobserved `reported_model_names` aliases are declared.

Run `bob-broker <lane>` as **bobapp inside the runtime**. It validates the lane,
reads its gateway port from `/var/lib/bob-portal/<lane>.json`, and execs the
stdlib portal broker over stdio with `/var/lib/bob-portal/<lane>.token` and
`--portal-approvals`. Root, bob and other users are
rejected. Outside the runtime the broker still defaults to
no token file. Set `BOB_BROKER_TOKEN_FILE` or pass `--token-file`; an explicit
`--token-file` takes precedence. Without either setting, the broker exits with
a configuration reason.

The provisioning URI prints once on creation, before activation, so save it even
if unit startup subsequently fails. List/health never print the URI, password,
shared keys or lane environment. If activation fails, the configuration remains
available for diagnosis and `systemctl enable --now bob-{core,gateway,bridge}@<lane>.service`.
Removal disables/stops all three units, removes both portal files and any old
lane-directory broker token, and retains lane data and port reservations;
list marks the lane `retained`. Add refuses an existing directory, including
retained data. No automatic reuse or data deletion is performed.

The optional GGUF is checksum-checked into `models/` atomically, then the embedder
unit is restarted. A missing model skips that unit through `ConditionPathExists`;
the lane can still serve `/health`, with embedding-based search unavailable.
`bob-health` prints every loaded system unit, detailed runtime unit states and
both `/health` responses for every configured lane. It exits nonzero for failed
required runtime units/endpoints; retained lanes and an intentionally absent
embedder model are excluded from the aggregate health result.

## Local checks

Run `bash -n` on `build.sh`, `smoke.sh`, `fetch-checked.sh` and `bin/bob-embedder`,
and shellcheck when installed. Compile `lib/*.py`, Python entrypoints and
`bobclaw-core/tests/test_runtime_lane.py` with the repository venv's Python.
Run the new tests from `bobclaw-core` with `.venv/bin/python -m pytest` using
the absolute interpreter path, then `bash test-my-bob.sh` if sockets are permitted.
The R1 expected suite counts are core 4191 passed/29 failed, gateway 363/3,
pipeline 132/0; additional runtime tests increase only the core passed count.
The full gate already has known baseline failures; report its actual exit and
counts, never relabel those failures as a green gate.

### Safe update refusal codes

The private update journal keeps only the exception class in `failure`, never
exception text. Known refusals additionally set `failure_code`, which is also
returned under `upgrade` by `bob-runtime --status`:

| Code | Reason |
| --- | --- |
| `runtime_too_old` | Execution boundary or pre-isolation lane identity is missing |
| `runtime_unhealthy` | The runtime health gate failed |
| `insufficient_space` | Archive staging or upgrade/rollback space check failed |
| `unsupported_layout` | Mounted roots, an unavailable external Python dependency, or overlapping payload-root layouts require migration |

Other failures have no `failure_code`. A staging refusal records a terminal
attempt without replacing the recovery pointer for the last actual upgrade.
No upgrade phases, health gates or rollback steps are skipped.
