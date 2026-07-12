# Contributing

Thanks for looking at BoB. This is a **v0.98** release — headless-first, desktop-GUI
(no browser UI) — so expect rough edges and a moving target.

## Project layout

```
bobclaw-core/            LangGraph engine (routing, faces, fan-out, council, memory, build, backends)
  core/                    the package
  tests/                   pytest suite
  docker/                  build-sandbox Dockerfile
bobclaw-gateway/         aiohttp gateway (auth, chat, REST + WS API — JSON only)
bobclaw-claude-pipeline/ Claude build-session wrapper
bobclaw-app/             Kotlin Multiplatform GUI (desktop; Android preview) — localized EN/简/繁
scripts/win/             Windows launchers + durability
docker-compose.yml       Postgres / Redis / Qdrant (loopback-only)
install-bob.ps1          guided setup   (see also AGENTS-SETUP.md)
```

## Dev setup

Prerequisites: Docker Desktop (running), [`uv`](https://docs.astral.sh/uv/),
Python 3.13. The fastest path is `./install-bob.ps1`. Manually:

```powershell
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -r bobclaw-core\requirements.lock
uv pip install --python .venv\Scripts\python.exe -r bobclaw-gateway\requirements.lock
uv pip install --python .venv\Scripts\python.exe -r bobclaw-claude-pipeline\requirements.lock
docker compose up -d postgres redis qdrant
```

**Dependency landmine:** `aiohttp` must stay `<3.14` (3.14 breaks the
`aioresponses` mock-patching the tests rely on). It is pinned in every
`requirements.txt` and in the `requirements.lock` files — keep it there.

## Running the tests

Each service's suite runs from its own directory against the shared venv, with
`PYTHONPATH` pointed at the service package:

```powershell
$py = "$PWD\.venv\Scripts\python.exe"
cd bobclaw-core           ; & $py -m pytest -q ; cd ..
cd bobclaw-gateway        ; $env:PYTHONPATH="..\bobclaw-core"; & $py -m pytest -q ; cd ..
cd bobclaw-claude-pipeline; & $py -m pytest -q ; cd ..
```

**Tests must not hit the network.** `pytest-socket` is enabled; mock
`aiohttp.ClientSession` (see `test_kimi_backend.py`) or use the injection seams
(`_send_to_backend`, `_router`) instead of real calls.

`./run_baseline_tests.ps1` runs the whole release surface (all three pytest suites,
the KMM shared JVM tests + desktop compile, and `pip check`) and exits non-zero if
any suite fails. The optional zvec provider surface skips unless `zvec` is installed
(`pip install zvec==0.5.1`).

## Conventions

- **Face IDs use hyphens** (`planner-claude`); **filenames use underscores**
  (`planner_claude.yaml`). Faces are validated by Pydantic in
  `core/faces/registry.py` (non-empty ID + system prompt).
- **Backend names are bare strings**, no enum: `local`, `claude_api`, `claude_code`,
  `deepseek_v4_flash`, `glm_5_2`, `minimax`, `gemini_flash`, `kimi_code`, `agy_code`,
  `codex_code`, `opencode_serve`, … Register a new backend string in
  `core/nodes/execute.py`.
- **New HTTP backends mirror `core/backends/kimi.py`**: async aiohttp,
  OpenAI-compatible `/v1/models` + `/v1/chat/completions`, optional bearer auth, and a
  `health_check()` that short-circuits (returns `False`) on missing config with no
  network call. (`claude_code` is the exception — a subprocess shape, not HTTP.)
- **The gateway wire contract is snake_case** everywhere; list endpoints return
  `{items, ...}` envelopes; WebSocket frames carry a top-level `type` discriminator.
- **Route returns a dict merged into state** — keep `face_id` / `backend` / posture
  writes consistent across every return path in `core/nodes/route.py`.

## Adding things

- **A backend:** copy `core/backends/kimi.py`, register the backend string in
  `core/nodes/execute.py`, add a face profile under `core/faces/profiles/`, and add a
  cost/width entry in `core/config.py`.
- **A face:** add a YAML under `core/faces/profiles/` (underscored filename, hyphenated
  `id`); the registry loads it and validates it.

## Merging

Gate order is **build → unit tests → a live/browser end-to-end run → then merge**.
Anything that touches a real subprocess backend (e.g. the `claude_code` path) should
be exercised with a real spawn before merge — live E2E catches what unit tests miss.

## Security-sensitive changes

Anything touching auth, the gateway→core scope vouch, the build/verify sandbox, or the
network-binding defaults should be reviewed against `SECURITY.md`. Do not weaken the
loopback defaults or the sandbox isolation without a very good reason and a clear
callout.
