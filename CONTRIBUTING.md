# Contributing

BoB 0.99.1 is a Windows desktop app preview with a Python server and a packaged
Linux runtime. Workflows and unattended runs remain previews. For installation
use [AGENTS-SETUP.md](AGENTS-SETUP.md); the commands below are for development.

## Project layout

| Directory | Contents |
| --- | --- |
| `bobclaw-core/` | LangGraph engine, agents/faces, routing, model backends, memory, Builder, worker clients, workflows and pytest tests. |
| `bobclaw-gateway/` | aiohttp authentication, REST/WebSocket APIs and pytest tests. |
| `portal/shell/` | React/TypeScript UI, Tauri Rust host in `src-tauri/`, and UI harnesses in `harness/`. |
| `portal/engine/` | Rust engine and integration tests; member of the `portal/` Cargo workspace. |
| `portal-broker/` | Server-side standard-input/output broker used by the desktop app. |
| `deploy/runtime/` | Runtime image, provisioning, systemd units, worker policy and Linux tests. |
| `bobclaw-tui/` | Textual terminal client and pytest tests. |
| `bobclaw-telegram/` | Telegram client of the gateway. |
| `bobclaw-claude-pipeline/` | Optional older Claude build-session service and its pytest suite. |
| `deploy/systemd/`, `scripts/` | Deployment templates and launch/install helpers. Some target older layouts; use the current install guide. |
| `init.sql`, `migrations/` | Postgres schema and journaled migrations. |
| `docker-compose.yml` | Loopback-published datastores; not the full application runtime. |
| `bobclaw-app/` | Earlier Kotlin app, incompatible with this release; retained until the mobile app is rebuilt. |

`install-bob.ps1` is a retired bootstrap that directs readers to the desktop
installer. It is not a developer environment installer.

## Python development environment

Use Python 3.12, matching the source installer, and a private virtual environment. From the repository
root on Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r bobclaw-core/requirements.txt \
  -r bobclaw-gateway/requirements.txt 'textual>=0.80,<2'
export BOB_DEV_ROOT="$PWD"
export BOB_DEV_PY="$BOB_DEV_ROOT/.venv/bin/python"
```

For Windows PowerShell, the environment setup differs:

```powershell
py -3.12 -m venv .venv
$env:BOB_DEV_ROOT = (Get-Location).Path
$env:BOB_DEV_PY = Join-Path $env:BOB_DEV_ROOT '.venv\Scripts\python.exe'
& $env:BOB_DEV_PY -m pip install -r bobclaw-core/requirements.txt -r bobclaw-gateway/requirements.txt 'textual>=0.80,<2'
```

The requirements files contain version ranges. Preserve the current
`aiohttp<3.14` constraint used by the HTTP mocks. Unit tests should mock provider
calls; do not use personal credentials or a production lane. The core/gateway
pytest configuration disables external sockets while allowing loopback.
Core integration tests are skipped unless explicitly selected.

## Python suites

Run each service from its directory so its modules and configuration resolve
correctly. These Linux subshells return to the repository root after each suite:

```bash
(cd bobclaw-core && "$BOB_DEV_PY" -m pytest tests -q)
(cd bobclaw-gateway && PYTHONPATH="$BOB_DEV_ROOT/bobclaw-core" \
  "$BOB_DEV_PY" -m pytest tests -q)
(cd bobclaw-tui && PYTHONPATH="$BOB_DEV_ROOT/bobclaw-core:$BOB_DEV_ROOT/bobclaw-tui" \
  "$BOB_DEV_PY" -m pytest tests -q)
```

PowerShell uses a semicolon between Python path entries and explicit directory
restoration:

```powershell
Push-Location bobclaw-core
try { & $env:BOB_DEV_PY -m pytest tests -q } finally { Pop-Location }
$env:PYTHONPATH = Join-Path $env:BOB_DEV_ROOT 'bobclaw-core'
Push-Location bobclaw-gateway
try { & $env:BOB_DEV_PY -m pytest tests -q } finally { Pop-Location }
$env:PYTHONPATH = "$env:BOB_DEV_ROOT\bobclaw-core;$env:BOB_DEV_ROOT\bobclaw-tui"
Push-Location bobclaw-tui
try { & $env:BOB_DEV_PY -m pytest tests -q } finally { Pop-Location }
Remove-Item Env:PYTHONPATH
```

For changes to the optional pipeline, install
`bobclaw-claude-pipeline/requirements.txt` in the same venv, then run its
`python -m pytest tests -q` from that directory.

Runtime tests are **Linux only**. From the root:

```bash
PYTHONPATH="$PWD/bobclaw-core:$PWD/deploy/runtime/lib" \
  .venv/bin/python -m pytest deploy/runtime/tests -q
```

Some runtime tests skip unless running as root because they exercise real ownership
and directory syscalls. On a disposable Linux test machine, run those with:

```bash
sudo env PYTHONPATH="$PWD/bobclaw-core:$PWD/deploy/runtime/lib" \
  "$PWD/.venv/bin/python" -m pytest deploy/runtime/tests -q
```

These pytest suites do not replace live kernel/systemd acceptance. The
`*_acceptance.py` programs are separate executable checks with their own
prerequisites; follow the runtime's deployment guides in a disposable runtime.
Do not run them against a working installation just to collect unit-test results.

### What to expect

Not every test passes on a clean checkout yet. Part of the suite was written against
the packaged runtime and assumes its users, paths and helper programs. Measured for
0.99.1 on a fresh Ubuntu 24.04 with Python 3.12, using the commands above:

| Suite | Passed | Failed or errored |
| --- | ---: | ---: |
| Core | 6510 | 64 |
| Gateway | 987 | 54 |
| Terminal client | 142 | 2 |
| Runtime, as an ordinary user | 220 | 66 |
| Runtime, as root | 361 | 1 |
| App engine (`portal/Cargo.toml`, Linux) | 193 | 32 |
| App host (`portal/shell/src-tauri`, Windows) | 169 | 0 |

Judge a change by comparison: run the suite on the unchanged tree first, then on your
change, and add no new failures. One app-host test,
`runtime_upgrade_staging_deadline_covers_blocked_pipe_writes`, is sensitive to timing
and can fail on a busy machine; it passes when run alone. Making every suite pass on a
clean checkout is planned before 1.0.

## Desktop app

Install Node/npm and Rust. The Tauri host also needs platform libraries. On Ubuntu:

```bash
sudo apt-get install build-essential pkg-config libwebkit2gtk-4.1-dev \
  libssl-dev libayatana-appindicator3-dev librsvg2-dev patchelf
```

From the repository root, the Rust suites are separate:

```bash
cargo test --manifest-path portal/Cargo.toml
cargo test --manifest-path portal/shell/src-tauri/Cargo.toml
```

The first runs the engine workspace, which builds on Linux only; the second runs the
native app host.
Platform-gated tests only run on their target OS. Windows additionally needs the
Rust MSVC toolchain, Microsoft C++ build tools and WebView2. The Cargo/npm commands
are otherwise the same.

Type checks and the UI harness run in `portal/shell`:

```bash
cd portal/shell
npm ci
npx tsc --noEmit
npx tsc --noEmit -p tsconfig.harness.json
npm run test:harness
```

`npm run test:harness` also runs its declared `pretest:harness` checks. It exercises
mocked DOM/transport behavior, not a live WSL import, SSH connection or provider
sign-in. For a production frontend build run `npm run build`; for the native app
build use `npm run tauri -- build`. A native build alone does not assemble a
Windows installer containing a matching runtime bundle.

## Changes and review

Keep model enrollment in lane configuration rather than introducing a private
deployment into defaults. Face IDs use hyphens; their YAML filenames use
underscores. Add new backends to the execution registry and relevant lane/provider
configuration, with mocked tests for errors and unavailable credentials.

Review authentication, scope forwarding, Builder/PC permissions, worker isolation
and listener changes against [SECURITY.md](SECURITY.md). Report the exact suites
and live checks run, including skips and platform limits. Use a disposable
installation for real subprocess, network, sign-in or runtime-upgrade checks.
Do not use the old baseline script as the release-wide gate: it still targets the
earlier Kotlin app.
