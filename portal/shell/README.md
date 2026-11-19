# bob-slice-shell — S10 webview shell (Tauri v2 + React/TS)

One window, one connection, one session, one turn, one call (`FX-READ-01`)
over the E5 slice-v1 NDJSON protocol. The Rust side (STALE-9 Tauri IPC
binding) spawns `bob-slice-cli` and forwards its stdout NDJSON lines to
the webview **verbatim** — no field added, renamed, dropped or re-ordered —
so the IPC and stdio bindings carry byte-equivalent payloads.

## Prerequisites

1. Engine binaries (built at the repo root, output under
   `target/debug/`): `bob-slice-cli.exe`, `fx-read.exe`

   ```
   cd \dev\projects\bob
   cargo build
   ```

   Override locations with `BOB_SLICE_CLI` / `BOB_FX_READ` if needed.
2. Node 18+ and npm.

## Run (dev mode)

```
cd shell
npm install
npm run tauri dev
```

## Release build

```
npm run tauri build -- --no-bundle
```

produces `shell/src-tauri/target/release/bob-slice-shell.exe`
(single launchable executable; bundling is disabled in `tauri.conf.json`).

## What the window does

- On "Open session and begin the turn": `hello` → `session.create` →
  `turn.start` (fixed request sequence, one line per request).
- Every frame is acked (`event.ack`) before it renders; the
  `approval.requested` frame is acked before the approval prompt appears.
- The approval card shows exactly the 9 `ApprovalPreview` fields
  (RULING-7). `allow_once` / `deny(reason?)` echo `call_id`,
  `decision_id` and `args_digest` back; a mismatched echo is rejected by
  the engine (`approval_mismatch`, request stays pending).
- "Verification" checkbox on the card corrupts the `args_digest` echo
  for the next response so the mismatch path can be demonstrated live.
- Tool output renders as inert text; the five `tool.result` statuses and
  four turn statuses render verbatim; `tool.started` is labeled the
  start witness ("authorized to attempt"), never "running".

## The wire protocol module is generated

`src/protocol.ts` is a **generated file** (A-10 / AT-S5-03 / EXEC-49). The
engine's normative wire schema (`engine/src/wire_schema.rs` — the same model
`hello`'s `schema_hash` is computed over) is the single source of truth; the
TypeScript types, the request/response shapes, the closed `WireErrorCode`
union and the S5 constants are rendered from it by `bob-wiregen`. Do not edit
it by hand.

```
npm run gen:protocol      # rewrite src/protocol.ts from the engine model
npm run check:protocol    # exit 1 if src/protocol.ts is stale
```

Drift is a test failure, not a review comment: `engine/tests/e11_codegen.rs`
regenerates the module inside `cargo test` and compares it byte-for-byte
(line endings normalized, because git checks out native), and the JS harness
case `generated_protocol_matches_the_hello_schema_hash` checks the generated
`WIRE_SCHEMA_HASH`/limits against the `HelloAck` in the committed wire
transcripts. `src/text.ts` holds the render helpers that are not wire types
(`asText`).

## Tests

`cargo test` in `shell/src-tauri` runs a scripted e2e battery through
the same bridge the webview commands use, against the real
`bob-slice-cli`/`fx-read` binaries: allow path (8 frames, exit 0), deny
with reason (7 frames, reason verbatim), corrupted echo
(`approval_mismatch`, nothing appended, retry succeeds), and
`version_mismatch` (exit 4).

`npm run test:harness` drives the real reducer + the generated protocol
module over the committed wire transcripts (see `harness/`).


## E12 Linux experimental preview (not a tier-1/a11y certification)

The fixed read-only fixture now runs inside real bubblewrap on Linux x86_64.
Preflight does not run a helper; it verifies and binds helper/payload bytes and
pins the fixture read-root before the durable witness. Bubblewrap gets only
`<data-root>/slice`, `/usr`, `/lib`, `/lib64`, `/bin`, plus a new `/proc` and
synthetic `/dev`; no host-root bind, writable host mount, or weaker fallback.
User/PID/IPC/network namespaces, capability dropping, and the x86_64 seccomp
ptrace/process_vm/io_uring denylist are exercised by `s4_exec` negative controls.
Other syscall ABIs fail closed. Windows remains `platform_deferred`; no Windows
security or accessibility certification is implied by its historical fixture path.

Linux launch prerequisite: `icons/icon.png` is supplied (derived from the
existing ICO). To embed the built frontend instead of expecting a Vite dev
server, use the command actually exercised in E12:

```sh
# At repository root
cargo build --all-targets
cd shell
npm ci
npm run build
npm run test:harness
npm run test:a11y       # real React/DOM semantics, NOT native AT acceptance
npm run check:protocol
cd src-tauri
cargo test
cargo build --features tauri/custom-protocol
```

Native, CPU-only evidence can be reproduced without touching the operator's
visible desktop (dependencies must already be installed; the harness installs
nothing and starts no global service):

```sh
# At repository root; supply a NEW user-cache artifact directory per run
python3 shell/harness/linux-atspi.py --artifacts "$HOME/.cache/bob-native-allow" --scenario allow
python3 shell/harness/linux-atspi.py --artifacts "$HOME/.cache/bob-native-deny" --scenario deny
python3 shell/harness/linux-atspi.py --artifacts "$HOME/.cache/bob-native-mismatch" --scenario mismatch
```

This starts private Xvfb, a private D-Bus session, private settings/runtime
folders, the shell and real Orca, with software rendering and speech rendered
by the espeak-ng module into a private ALSA `null` device (no PulseAudio, no
operator audio). It records native keyboard activation, AT-SPI roles/names/states,
result text, screenshots, and Orca's trace, then tears its children down.
The denial flow uses Tab → deny → reason → Tab → submit. The mismatch flow
proves the approval remains pending and a corrected response can finish.

**Remaining hard gate:** with a live reader and a working speech sink
(`probe_state: MEASURED`, `reader_live_at_result: true`), Orca still did not
generate the outcome announcement, so the gate is **measurably red** rather
than unmeasurable. `--require-live-utterance` is intentionally strict: it
refuses to judge when the probe is invalid and fails when a live reader stayed
silent. It is not counted as a passed native acceptance check. The status text
is present in AT-SPI and its DOM has polite/atomic live semantics. No human
listened to synthesized speech (`human_intelligibility: NOT_TESTED`). Full
a11y/tier-1 acceptance remains **PARTIAL**. No degraded rendering
or launch-admission matrix, performance certification, arbitrary-command UI,
live model call, or deployment is part of this stage.

## MVP1 local preview (launcher + headless demo)

`scripts/linux/run-preview.sh` is the low-friction entry point for this shell:
it checks prereqs one binary at a time, proves bubblewrap namespaces really
work, builds engine + frontend + shell (`--features tauri/custom-protocol`,
so the window serves the embedded build and needs no dev server), and launches
the shell with a **fresh per-run data root** under
`${XDG_CACHE_HOME:-~/.cache}/bob-preview/runs/`. It never uses sudo, never
installs a system package, never listens on a port and never relaxes the
sandbox. `--headless` runs the real CLI demo instead. Full usage, typed exit
codes, ledger locations and the honest limits are in the repository's
`TINKER.md`.

`BOB_PREVIEW_DATA_ROOT` (read by `slice_start` when the caller passes no root,
`bridge::data_root_for`) is what makes the preview's ledger land in that
documented run directory instead of a per-process temp dir.

The native harness can drive the launcher itself, which is how the MVP1 GUI
evidence was produced:

```sh
python3 shell/harness/linux-atspi.py --artifacts ~/.cache/bob-native-launcher \
  --scenario allow --launch "$PWD/scripts/linux/run-preview.sh --no-build"
```

