"""
BoBClaw Core — Configuration
Loads from secure .secrets path with fallback to local .env
"""
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load env: check .secrets first, then local
_SECURE_ENV = Path(__file__).parent.parent.parent / ".secrets" / "bobclaw.env"
_LOCAL_ENV = Path(__file__).parent.parent / ".env"

# override=False: the real process environment (service launchers, and pytest's
# conftest os.environ.setdefault) wins over the .secrets file. This keeps .secrets
# from leaking into pytest and breaking baselines, and lets launcher env take
# precedence at runtime. (Matches bobclaw-claude-pipeline/config.py.)
if _SECURE_ENV.exists():
    load_dotenv(_SECURE_ENV, override=False)
else:
    load_dotenv(_LOCAL_ENV, override=False)


def _default_cli_scratch_root(name: str) -> str:
    """Platform-native default scratch root for a CLI backend's spawn dirs.

    Windows keeps the original ``C:/dev/scratch/<name>`` layout (the paths every
    existing Windows deployment was seeded with); POSIX gets a per-user
    ``~/.bobclaw/scratch/<name>`` — a bare ``C:/...`` default on Linux resolves
    as a RELATIVE path and silently sprays ``C:/`` dirs under whatever cwd the
    service started from. Created on demand by the clients, not here.
    """
    if os.name == "nt":
        return f"C:/dev/scratch/{name}"
    return os.path.join(os.path.expanduser("~"), ".bobclaw", "scratch", name)


class BoBClawConfig:
    """Core service configuration."""

    # ── Server ────────────────────────────────────────────
    PORT: int = int(os.getenv("BOBCLAW_CORE_PORT", "7825"))
    # Loopback-only by default (R1 network containment). Only the authenticated
    # gateway is ever intentionally exposed, behind the TLS/SSH boundary in
    # docs/SECURITY.md. Override with BOBCLAW_CORE_HOST=0.0.0.0 to bind all interfaces.
    HOST: str = os.getenv("BOBCLAW_CORE_HOST", "127.0.0.1")

    # ── Database ──────────────────────────────────────────
    POSTGRES_URL: str = os.getenv(
        "POSTGRES_URL", "postgresql://bobclaw:bobclaw@localhost:5432/bobclaw"
    )

    # ── Local Model Backends ──────────────────────────────
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    LMSTUDIO_URL: str = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
    # No default name — set per environment; core code carries no model names.
    PREFERRED_LOCAL_MODEL: str = os.getenv("PREFERRED_LOCAL_MODEL", "")

    # ── Cloud Backends ────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_BASE_URL: str = os.getenv(
        "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
    )
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")

    # ── Gemini (cloud, REST API) ──────────────────────────
    GEMINI_FLASH_MODEL: str = os.getenv("GEMINI_FLASH_MODEL", "gemini-3-flash-preview")
    GEMINI_PRO_MODEL: str = os.getenv("GEMINI_PRO_MODEL", "gemini-3.1-pro-preview")
    GEMINI_DEEP_RESEARCH_MODEL: str = os.getenv("GEMINI_DEEP_RESEARCH_MODEL", "gemini-3.1-pro-preview")

    # ── Kimi (coding) ─────────────────────────────────────
    # Membership HTTP endpoint (api.moonshot.ai). The CLI/IDE slug
    # "kimi-for-coding" is NOT a valid API model ID; use the real model name.
    KIMI_API_KEY: str = os.getenv("KIMI_API_KEY", "")
    KIMI_BASE_URL: str = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    KIMI_MODEL: str = os.getenv("KIMI_MODEL", "kimi-k2.7-code")

    # ── DeepSeek (cloud, OpenAI-compat) ──────────────────
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    # DS Pro (senior/audit tier) — the `deepseek_v4` backend. Confirmed id 2026-07-04.
    DEEPSEEK_PRO_MODEL: str = os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")

    # ── Z.AI / GLM-5.2 (cloud, OpenAI-compat) ─────────────
    # Base is `.../paas/v4` (NOT `/v1`); the client appends `/chat/completions`.
    # ONE key, TWO surfaces: `api/paas/v4` bills PAYG BALANCE (429 code 1113 when empty);
    # `api/coding/paas/v4` is the GLM Coding Plan subscription. Default = coding (our plan).
    ZAI_API_KEY: str = os.getenv("ZAI_API_KEY", "")
    ZAI_BASE_URL: str = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    ZAI_MODEL: str = os.getenv("ZAI_MODEL", "glm-5.2")

    # ── Qwen research floor (LOCAL llama.cpp, OpenAI-compat, no auth) — MS2-R0 ──
    # Self-hostable agentic research head (Qwen 35B-A3B Q4 expert-split, DESIGN-MS-D2 §3/OD#1).
    # A local llama-server needs NO API key; the adapter's health is reachability-based, not
    # key-gated. The longer chat timeout covers a multi-turn local tool-calling loop.
    QWEN_RESEARCH_BASE_URL: str = os.getenv("QWEN_RESEARCH_BASE_URL", "http://127.0.0.1:8091/v1")
    QWEN_RESEARCH_MODEL: str = os.getenv("QWEN_RESEARCH_MODEL", "qwen3.6-35b-a3b")
    QWEN_RESEARCH_API_KEY: str = os.getenv("QWEN_RESEARCH_API_KEY", "")  # local server: normally empty
    QWEN_RESEARCH_TIMEOUT_S: float = float(os.getenv("QWEN_RESEARCH_TIMEOUT_S", "600"))

    # ── MiniMax M3 (cloud, OpenAI-compat) ────────────────
    # MiniMax M3 — senior reasoning tier (backend: core/backends/minimax.py).
    MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")
    MINIMAX_BASE_URL: str = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    MINIMAX_MODEL: str = os.getenv("MINIMAX_MODEL", "MiniMax-M3")

    # ── Kimi Platform (PAYG fallback) ─────────────────────
    MOONSHOT_API_KEY: str = os.getenv("MOONSHOT_API_KEY", "")
    KIMI_PLATFORM_BASE_URL: str = os.getenv(
        "KIMI_PLATFORM_BASE_URL", "https://api.moonshot.cn/v1"
    )
    KIMI_PLATFORM_MODEL: str = os.getenv(
        "KIMI_PLATFORM_MODEL", "kimi-k2.6"
    )
    KIMI_PLATFORM_DAILY_USD_LIMIT: float = float(
        os.getenv("KIMI_PLATFORM_DAILY_USD_LIMIT", "20.00")
    )
    KIMI_PLATFORM_DAILY_USD_WARN: float = float(
        os.getenv("KIMI_PLATFORM_DAILY_USD_WARN", "10.00")
    )

    # ── Claude Code (subprocess CLI, planning tier) ──────
    # Opt-in (NOT in validate()). No API key on this path — the genuine
    # ``claude`` CLI runs under the user's subscription OAuth login.
    # CC_CLI_PATH unset → resolve ``claude`` on PATH.
    # CC_PROJECT_DIR defaults to the bobclaw repo root (two levels up from
    # this file: core/ -> bobclaw-core/ -> repo root, where CLAUDE.md lives).
    CC_CLI_PATH: str = os.getenv("CC_CLI_PATH", "")
    CC_PROJECT_DIR: str = os.getenv(
        "CC_PROJECT_DIR",
        str(Path(__file__).resolve().parent.parent.parent),
    )
    CC_TIMEOUT_SECONDS: int = int(os.getenv("CC_TIMEOUT_SECONDS", "300"))
    # Scratch root for the scratch-write planner posture (C2.1). The per-
    # conversation scratch dir is CC_SCRATCH_ROOT/<conversation_id>; it is the
    # subprocess cwd for scratch-write spawns so ideation tools land OUTSIDE the
    # repo. It MUST be outside CC_PROJECT_DIR — when scratch lived under the
    # repo, the Write(<repo>/**) deny blocked scratch writes too (manager probe
    # 2026-06-15). Opt-in; NOT in validate().
    CC_SCRATCH_ROOT: str = os.getenv("CC_SCRATCH_ROOT", _default_cli_scratch_root("cc"))
    # Optional segregated claude CLI config home — the CLAUDE_CONFIG_DIR analog
    # of AGY_HOME / CODEX_HOME (spawn-context intake 2026-07-18). When set AND
    # the dir exists, every claude_code spawn runs with CLAUDE_CONFIG_DIR=<dir>
    # so headless spawns never read the interactive user's ~/.claude state
    # (settings, skills, MCP registrations, memory). Unset ⇒ inherit the real
    # config home (subscription auth lives there until a seeded dir carries it).
    CC_CONFIG_DIR: str = os.getenv("CC_CONFIG_DIR", "")
    # Sidecar JSONL for the session_id -> conversation_id mapping (C3); the LKS
    # transcript adapter reads it. Opt-in; NOT in validate().
    CC_SIDECAR_PATH: str = os.getenv(
        "CC_SIDECAR_PATH",
        str(
            Path(__file__).resolve().parent.parent
            / "data"
            / "cc_session_sidecar.jsonl"
        ),
    )
    # C4 feature gate: when False (default), an approved ``cc_edit`` is captured
    # + surfaced but NOT applied to the repo (the apply is a no-op with a note).
    # When True, an approved ``cc_edit`` is applied via core-direct ``git apply``
    # (check-first, whole-or-nothing, no commit). Pure on/off — the mechanism is
    # settled. Opt-in; NOT in validate().
    CC_EDIT_APPLY_ENABLED: bool = (
        os.getenv("CC_EDIT_APPLY_ENABLED", "false").lower() in ("1", "true", "yes")
    )

    # ── Antigravity (agy CLI, Gemini "Second Voice" tier) ─
    # Opt-in (NOT in validate()). No API key on this path — the genuine ``agy``
    # CLI runs under the user's Google subscription login. The metered REST twin
    # is the SEPARATE ``gemini_pro`` backend (the escalation target).
    # AGY_CLI_PATH: on Windows agy installs to a known AppData path and is NOT
    # on PATH, so that absolute path is the default. On POSIX there is no fixed
    # install location — default empty ⇒ the client falls back to resolving
    # ``agy`` on PATH; set AGY_CLI_PATH explicitly when it lives elsewhere.
    AGY_CLI_PATH: str = os.getenv(
        "AGY_CLI_PATH",
        os.path.join(
            os.path.expanduser("~"), "AppData", "Local", "agy", "bin", "agy.exe"
        )
        if os.name == "nt"
        else "",
    )
    AGY_PROJECT_DIR: str = os.getenv(
        "AGY_PROJECT_DIR",
        str(Path(__file__).resolve().parent.parent.parent),
    )
    AGY_TIMEOUT_SECONDS: int = int(os.getenv("AGY_TIMEOUT_SECONDS", "300"))
    # Per-conversation scratch dir is AGY_SCRATCH_ROOT/<conversation_id>; it is the
    # subprocess cwd for scratch-write spawns and the only path the strict
    # settings.json allows writes to. MUST be outside AGY_PROJECT_DIR.
    AGY_SCRATCH_ROOT: str = os.getenv("AGY_SCRATCH_ROOT", _default_cli_scratch_root("agy"))
    # Segregated BoBClaw-owned agy home. When set AND the dir exists, every agy
    # spawn runs with USERPROFILE=AGY_HOME *and* HOME=AGY_HOME (Windows CLIs read
    # USERPROFILE, POSIX CLIs read HOME) so agy reads AGY_HOME/.gemini/... — a
    # strict (no-shell, write-only-scratch) settings.json that NEVER touches the
    # user's real interactive agy. Unset / not-yet-seeded ⇒ inherit the real home
    # (planner-only fallback). Seeded by the A2 auth-carryover step.
    AGY_HOME: str = os.getenv("AGY_HOME", _default_cli_scratch_root("agy-home"))
    # Documentation / health only — agy has no --settings flag; the file is global
    # within whichever home the spawn env resolves to. Default FOLLOWS AGY_HOME
    # (the old default hardcoded the Windows path even when AGY_HOME was moved).
    AGY_SETTINGS_PATH: str = os.getenv(
        "AGY_SETTINGS_PATH",
        os.path.join(AGY_HOME, ".gemini", "antigravity-cli", "settings.json"),
    )

    # ── Codex CLI (subprocess `codex exec`, glm/ds/qwen via LiteLLM) ─────
    # Opt-in (NOT in validate()). No API key here — `codex exec` runs under the
    # local LiteLLM proxy (LITELLM_BASE_URL) which holds each provider's key.
    # CODEX_CLI_PATH unset → resolve `codex` on PATH.
    CODEX_CLI_PATH: str = os.getenv("CODEX_CLI_PATH", "")
    CODEX_PROJECT_DIR: str = os.getenv(
        "CODEX_PROJECT_DIR",
        str(Path(__file__).resolve().parent.parent.parent),
    )
    CODEX_TIMEOUT_SECONDS: int = int(os.getenv("CODEX_TIMEOUT_SECONDS", "300"))
    # Per-conversation scratch cwd = CODEX_SCRATCH_ROOT/<conversation_id>; the
    # codex spawn's working root + where the -o reply file is written. Outside the repo.
    CODEX_SCRATCH_ROOT: str = os.getenv("CODEX_SCRATCH_ROOT", _default_cli_scratch_root("codex"))
    # Optional segregated CODEX_HOME (a BoBClaw-owned ~/.codex with the provider
    # profiles) so spawns don't touch the user's interactive codex config. Unset /
    # missing ⇒ inherit the real CODEX_HOME (uses the user's profiles).
    CODEX_HOME: str = os.getenv("CODEX_HOME", "")
    # The local LiteLLM proxy codex routes through. codex_code.health_check probes
    # it — codex is dead without it (all providers route through it). The JOAT
    # health-walk uses this to route codex_code → opencode_serve when :4000 is down.
    # 127.0.0.1, NOT localhost — aiohttp may resolve localhost→::1 and miss the
    # IPv4-bound proxy (the project-wide IPv4 rule; codex's own config can keep
    # localhost since its HTTP client handles both).
    LITELLM_BASE_URL: str = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000")

    # ── Kimi CLI (subprocess `kimi -p`, membership login) ─────
    # Opt-in (NOT in validate()). DISTINCT from kimi_code (HTTP) and codex's -p kimi
    # — Kimi runs through its own CLI. CLI auth via `kimi login`; no API key here.
    # KIMI_CLI_PATH unset → resolve `kimi` on PATH.
    KIMI_CLI_PATH: str = os.getenv("KIMI_CLI_PATH", "")
    KIMI_CLI_PROJECT_DIR: str = os.getenv(
        "KIMI_CLI_PROJECT_DIR",
        str(Path(__file__).resolve().parent.parent.parent),
    )
    KIMI_CLI_TIMEOUT_SECONDS: int = int(os.getenv("KIMI_CLI_TIMEOUT_SECONDS", "300"))
    # Clean scratch cwds for kimi spawn-context spawns (kimi had no scratch root
    # before — it spawned from the repo cwd, the exact charter-leak the
    # spawn-context seam closes).
    KIMI_CLI_SCRATCH_ROOT: str = os.getenv(
        "KIMI_CLI_SCRATCH_ROOT", _default_cli_scratch_root("kimi")
    )

    # ── Spawn-context injection (per-position CLI spawn framing) ─────
    # Templates rendered into each CLI spawn's scratch cwd as its context file
    # (CLAUDE.md / AGENTS.md / GEMINI.md) — position/role framing, task, and the
    # "do not adopt the host charter" constraints. Data, not code: tune the
    # framing by editing the templates. See core/spawn_context.py.
    SPAWN_CONTEXT_DIR: str = os.getenv(
        "SPAWN_CONTEXT_DIR",
        str(Path(__file__).resolve().parent.parent / "config" / "spawn_contexts"),
    )

    # ── Pi CLI (subprocess `pi -p`, agentic harness for GLM/DeepSeek/MiniMax) ──
    # Opt-in (NOT in validate()). The AGENTIC tool-loop harness for the OPEN
    # providers, replacing codex's fragile LiteLLM-proxy path. pi speaks each
    # provider's OpenAI-compat API DIRECTLY; provider config lives in
    # ~/.pi/agent/models.json (providers zai/deepseek/minimax) and the per-provider
    # keys are injected into the child env by the adapter from ZAI/DEEPSEEK/MINIMAX
    # keys above — no API key on this config line. pi ships no .exe (npm gives
    # pi.cmd/pi.ps1 + dist/cli.js); the adapter spawns `node <cli.js>`. PI_CLI_PATH,
    # when set, is the absolute path to that cli.js; unset → resolve it next to the
    # `pi` shim on PATH.
    PI_CLI_PATH: str = os.getenv("PI_CLI_PATH", "")
    PI_PROJECT_DIR: str = os.getenv(
        "PI_PROJECT_DIR",
        str(Path(__file__).resolve().parent.parent.parent),
    )
    PI_TIMEOUT_SECONDS: int = int(os.getenv("PI_TIMEOUT_SECONDS", "300"))
    # Per-conversation scratch cwd = PI_SCRATCH_ROOT/<conversation_id>; pi's working
    # root (only relevant when a posture enables tools). Outside the repo.
    PI_SCRATCH_ROOT: str = os.getenv("PI_SCRATCH_ROOT", "C:/dev/scratch/pi")

    # ── Profiles Scheduler (P5) ───────────────────────────
    # A profile carrying a ``schedule.cron`` can run unattended on a cron. Opt-in,
    # DEFAULT OFF (same posture as CC_EDIT_APPLY_ENABLED); NOT in validate(). Run by
    # ONE dedicated daemon (scripts/profile_scheduler.py) so the exactly-once ledger
    # lock is the only contention guard needed.
    PROFILE_SCHEDULE_ENABLED: bool = (
        os.getenv("PROFILE_SCHEDULE_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    # Poll cadence (s). Cron granularity is 1 minute, so 60 is plenty; lower only
    # tightens fire latency at the cost of more idle ticks.
    PROFILE_POLL_SECONDS: int = int(os.getenv("PROFILE_POLL_SECONDS", "60"))
    # Exactly-once fire ledger (INSERT OR IGNORE on (profile, fire_bucket)). Its own
    # tiny SQLite file so the scheduler does NOT depend on MEMORY_ENABLED.
    PROFILE_SCHEDULER_DB: str = os.getenv(
        "PROFILE_SCHEDULER_DB", ".memory/bobclaw_scheduler.db"
    )
    # Flight substrate (Lane 1a) — the flight supervisor's registry (Flight records:
    # name/project/budget_usd/priority/status). Its own tiny SQLite file, independent of
    # MEMORY_ENABLED (mirrors PROFILE_SCHEDULER_DB). Live per-flight spend lives in Redis
    # (core.telemetry.spend); this holds the durable control-plane metadata.
    FLIGHT_DB: str = os.getenv("FLIGHT_DB", ".memory/bobclaw_flights.db")
    # Flight substrate (Lane 1a / FU1) — LIVE hot-path enforcement master switch (default
    # OFF, same posture as T1_FASTPATH_ENABLED / CC_EDIT_APPLY_ENABLED; NOT in validate()).
    # When off, the GLM ProviderSlots mutex, the over-budget pause status-gate, and the
    # budget-enforcer daemon are all inert — dispatch/execute are byte-identical. Flip only
    # after the live GLM E2E + Travis OK (Gate A). Named flights only; ambient/live-face
    # work is never gated regardless.
    FLIGHT_ENFORCE_ENABLED: bool = (
        os.getenv("FLIGHT_ENFORCE_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    # Budget-enforcer daemon poll cadence (s). Mirrors PROFILE_POLL_SECONDS; enforce_budget
    # is idempotent so a coarse cadence is fine (pausing an already-paused flight is a no-op).
    FLIGHT_ENFORCE_POLL_SECONDS: int = int(os.getenv("FLIGHT_ENFORCE_POLL_SECONDS", "60"))
    # T1 flight-aware routing (Lane 1b) — the cross-project consensus ledger (a profile
    # promotes to the GLOBAL namespace tier only after crystallizing in >= N distinct
    # projects). Its own tiny SQLite file; INSERT OR IGNORE exactly-once per (job_shape,
    # project). See core.mcp_tools.profile_consensus.
    T1_PROFILE_DB: str = os.getenv("T1_PROFILE_DB", ".memory/bobclaw_t1_profiles.db")
    # Consensus threshold: distinct projects a job-shape must crystallize in before the
    # profile is promoted to the global tier (cross-contamination guard).
    T1_GLOBAL_PROMOTE_PROJECTS: int = int(os.getenv("T1_GLOBAL_PROMOTE_PROJECTS", "2"))
    # T1 fast-path master switch (default OFF — inert until profiles crystallize + Travis
    # tunes the class→face taxonomy + ε policy, SPEC §8). When off, route_node is
    # byte-identical (never consults the profile cache).
    T1_FASTPATH_ENABLED: bool = os.getenv("T1_FASTPATH_ENABLED", "false").lower() in ("1", "true", "yes")
    # ε exploration-valve rate (F1): on a servable hit, skip the fast path with this prob.
    T1_FASTPATH_EPSILON: float = float(os.getenv("T1_FASTPATH_EPSILON", "0.10"))
    # Catch-up window (s): a tick only fires a cron bucket whose scheduled time is
    # within this many seconds of now. Prevents a long-past bucket from firing when
    # the daemon starts fresh / wakes from sleep (no backfill of missed runs). Keep
    # it comfortably above PROFILE_POLL_SECONDS so a bucket is never missed between
    # ticks; default = 2x the default poll.
    PROFILE_SCHEDULE_CATCHUP_SECONDS: int = int(
        os.getenv("PROFILE_SCHEDULE_CATCHUP_SECONDS", "120")
    )
    # Max scheduled runs in flight at once. Fires are claim-then-SPAWN (the poll
    # loop never blocks on a slow council), bounded by this semaphore so many
    # co-due profiles don't stampede the backends.
    PROFILE_FIRE_CONCURRENCY: int = int(os.getenv("PROFILE_FIRE_CONCURRENCY", "4"))
    # Retention horizon (days) for the exactly-once fire ledger. Rows older than the
    # catch-up window can never be re-claimed; pruned each tick to keep the DB small.
    PROFILE_FIRE_RETENTION_DAYS: int = int(os.getenv("PROFILE_FIRE_RETENTION_DAYS", "7"))
    # Surface scheduled-run output: persist each fire to a Postgres conversation so
    # it's visible in the web UI / KMM (the same conversations table they read).
    # Best-effort — auto-disabled with a warning if Postgres is unreachable.
    PROFILE_SCHEDULE_PERSIST: bool = (
        os.getenv("PROFILE_SCHEDULE_PERSIST", "true").lower() in ("1", "true", "yes")
    )
    # Owner (user_id) of scheduled-run conversations. Single-user default 'admin';
    # a schedule may override per-profile via its ``owner`` key.
    PROFILE_SCHEDULE_DEFAULT_OWNER: str = os.getenv(
        "PROFILE_SCHEDULE_DEFAULT_OWNER", "admin"
    )

    # ── MCP (read-only stdio servers) ─────────────────────
    # Default: one filesystem-read server scoped to MCP_SCOPED_DIR.
    # Only read tools are allowlisted by faces; write/delete tools are NOT
    # wired in P1. The default server uses npx; override MCP_SERVERS with a
    # JSON dict if npx is unavailable in the deployment environment.
    MCP_SCOPED_DIR: str = os.getenv("MCP_SCOPED_DIR", CC_SCRATCH_ROOT)
    MCP_SERVERS: dict = json.loads(
        os.getenv(
            "MCP_SERVERS",
            json.dumps(
                {
                    "filesystem": {
                        "command": "npx",
                        "args": [
                            "-y",
                            "@modelcontextprotocol/server-filesystem",
                            MCP_SCOPED_DIR,
                        ],
                        "transport": "stdio",
                    }
                }
            ),
        )
    )

    # ── Auth ──────────────────────────────────────────────
    BOBCLAW_SECRET: str = os.getenv("BOBCLAW_SECRET", "")

    # ── Logging ───────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ── OpenCode workers ──────────────────────────────────
    OPENCODE_INSTANCES: str = os.getenv("OPENCODE_INSTANCES", "")
    # comma-separated host:port:workspace_dir triples
    OPENCODE_DEFAULT_TIMEOUT_S: int = int(
        os.getenv("OPENCODE_DEFAULT_TIMEOUT_S", "300")
    )
    OPENCODE_HEALTH_PROBE_INTERVAL_S: int = int(
        os.getenv("OPENCODE_HEALTH_PROBE_INTERVAL_S", "60")
    )
    # Model the serve seat is pinned to PER REQUEST, as ``providerID/modelID``.
    # ``opencode serve`` does NOT pick a model itself — WITHOUT this pin it answers
    # with the instance's config-default model, which in this environment is a LOCAL
    # llama.cpp model. That is the "opencode silently serves local" failure: the
    # client sends the model on every /message so a Copilot GPT seat is guaranteed
    # cloud GPT, never a silent local fallback. Default = the GitHub Copilot GPT seat
    # (Travis's target: opencode → Copilot plan). Empty ⇒ send NO model (legacy;
    # WILL serve the instance default — only set empty deliberately).
    OPENCODE_SEAT_MODEL: str = os.getenv("OPENCODE_SEAT_MODEL", "github-copilot/gpt-5.3-codex")

    # ── Memory Module ────────────────────────────────────
    MEMORY_ENABLED: bool = os.getenv("MEMORY_ENABLED", "false").lower() == "true"
    MEMORY_L1_EXTRACTION_ENABLED: bool = (
        os.getenv("MEMORY_L1_EXTRACTION_ENABLED", "false").lower() == "true"
    )
    # W3 T0 writer pin.  The writer core is inert unless explicitly enabled;
    # within an enabled writer, project_verbatim is the default-ON task and all
    # LLM/derived tasks remain default-OFF stubs.  Bootstrap additionally
    # requires the single-writer fence to be armed.
    MEMORY_WRITER_ENABLED: bool = (
        os.getenv("MEMORY_WRITER_ENABLED", "false").strip().lower() == "true"
    )
    # Read-side pin is independent so replay can be completed and audited before
    # T0 chunks are admitted to the live "Prior context" splice.
    MEMORY_T0_RECALL_ENABLED: bool = (
        os.getenv("MEMORY_T0_RECALL_ENABLED", "false").strip().lower() == "true"
    )
    MEMORY_SQLITE_PATH: str = os.getenv(
        "MEMORY_SQLITE_PATH", ".memory/bobclaw_memory.db"
    )
    # Default to BoB's OWN Qdrant (:6353, the compose host port + what the launchers
    # export), NOT the shared LKS Qdrant (:6333). A non-empty default means validate()
    # can't catch a missing value, so the default itself must be fail-safe: enabling
    # memory without an explicit MEMORY_QDRANT_URL must never write to LKS. C6
    # consolidation (when deployed) repoints this explicitly via env / MEMORY_SINGLE_QDRANT.
    MEMORY_QDRANT_URL: str = os.getenv(
        "MEMORY_QDRANT_URL", "http://localhost:6353"
    )
    MEMORY_STORES_CONFIG_PATH: str = os.getenv(
        "MEMORY_STORES_CONFIG_PATH", "config/memory_stores.toml"
    )
    MEMORY_DEFAULT_STORE_ID: str = os.getenv(
        "MEMORY_DEFAULT_STORE_ID", "bobclaw_default"
    )
    # MS2-C5 strangler cut-over (OD#4): recall reads the resolved LKS corpus collection THROUGH the C3
    # adapter FIRST, then falls back to BoB's own store — opt-in, DEFAULT OFF (flag off ⇒ recall is
    # byte-identical to today). MEMORY_QDRANT_URL is deliberately NOT changed here (that repoint + retiring
    # the duplicate store is C6); MEMORY_LKS_QDRANT_URL only repoints the LKS READ client (empty ⇒ reuse the
    # provider's client). MEMORY_LKS_INSTANCE names the federation instance to read LKS-first.
    # .strip().lower() == "true" — the SAME parse the bootstrap seam (_maybe_build_lks_adapter) applies, so
    # the config attribute and the seam agree on the flag's effective state even with surrounding whitespace
    # (audit r5: a " true " must not be ON in one place and OFF in the other).
    MEMORY_LKS_FIRST: bool = os.getenv("MEMORY_LKS_FIRST", "false").strip().lower() == "true"
    MEMORY_LKS_INSTANCE: str = os.getenv("MEMORY_LKS_INSTANCE", "")
    MEMORY_LKS_QDRANT_URL: str = os.getenv("MEMORY_LKS_QDRANT_URL", "")
    # MS2-C6 consolidation (OD#3 = one Qdrant :6333, registry-resolved). When ON, the converged memory path is
    # provably single-endpoint: MEMORY_LKS_QDRANT_URL must be empty or == MEMORY_QDRANT_URL (a differing value is
    # the two-Qdrant footgun → fail-closed at bootstrap), AND the C4 single-writer write fence is FORCED ON (BoB
    # writes only its own collection; corpus collections stay read-only). DEFAULT OFF ⇒ byte-identical to C5 (the
    # C4/C5 flags govern independently). The MEMORY_QDRANT_URL default is BoB's own :6353 (fail-safe, above);
    # C6 consolidation onto a single endpoint is an explicit operator repoint + data migration, never silently
    # flipped. .strip().lower() == "true" — the same parse as the other MEMORY_* flags (the config attribute and
    # the bootstrap seam must agree on the flag's effective state even with surrounding whitespace).
    MEMORY_SINGLE_QDRANT: bool = os.getenv("MEMORY_SINGLE_QDRANT", "false").strip().lower() == "true"

    # ── Ask-Bob helper bubble page context (MS9 U5, SPEC §3 / D3) ─────────
    # The "Ask Bob" helper bubble sends an additive ``page_context`` field on the chat
    # start-turn frame; the gateway forwards it and execute_node splices it as a
    # front-adjacent system card (the identity-card pattern — same shape as the project
    # context splice). FLAG-GATED and DEFAULT OFF: flag off ⇒ page_context_card() returns
    # None ⇒ the assembled prompt is BYTE-IDENTICAL to today regardless of what the client
    # sent (U5 accept criterion #1). NOT in validate() — same posture as CC_EDIT_APPLY_ENABLED /
    # T1_FASTPATH_ENABLED. .strip().lower() so a padded " true " agrees everywhere.
    PAGE_CONTEXT_ENABLED: bool = (
        os.getenv("PAGE_CONTEXT_ENABLED", "false").strip().lower() in ("1", "true", "yes")
    )

    # ── Redis ────────────────────────────────────────────
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # ── Gate Router (Phase 2) ─────────────────────────────
    # Default senior/auditor backend used when a scoped job's Gate decision is
    # ambiguous ("gate") and no per-call critic_backend override is provided.
    GATE_CRITIC_BACKEND: str = os.getenv("GATE_CRITIC_BACKEND", "minimax")

    # Stand-in critic: when the primary critic backend HARD-fails (timeout / HTTP error
    # such as Z.AI GLM's balance-exhausted 429), `run_critic` retries once on this healthy
    # backend so the fleet's critique is not silently lost. Empty string disables fallback.
    CRITIC_FALLBACK_BACKEND: str = os.getenv("CRITIC_FALLBACK_BACKEND", "deepseek_v4_flash")

    # (COUNCIL_MODE_DEFAULT lives at module level with the other COUNCIL_* constants.)

    @classmethod
    def opencode_instances_parsed(cls) -> list[tuple[str, int, str]]:
        """Parse OPENCODE_INSTANCES env var into (host, port, workspace_dir) triples."""
        results: list[tuple[str, int, str]] = []
        for part in cls.OPENCODE_INSTANCES.split(","):
            part = part.strip()
            if not part:
                continue
            pieces = part.split(":")
            if len(pieces) >= 3:
                host = pieces[0]
                port = int(pieces[1])
                workspace_dir = ":".join(pieces[2:])
                results.append((host, port, workspace_dir))
        return results

    # ── Platform Detection ────────────────────────────────
    IS_LINUX: bool = sys.platform == "linux"
    IS_WINDOWS: bool = sys.platform == "win32"

    @classmethod
    def validate(cls):
        """Validate required configuration."""
        errors = []
        if not cls.POSTGRES_URL:
            errors.append("POSTGRES_URL is required")
        if cls.MEMORY_ENABLED:
            if not cls.MEMORY_QDRANT_URL:
                errors.append("MEMORY_QDRANT_URL is required when MEMORY_ENABLED=true")
            if not cls.MEMORY_SQLITE_PATH:
                errors.append("MEMORY_SQLITE_PATH is required when MEMORY_ENABLED=true")
            if not cls.MEMORY_STORES_CONFIG_PATH:
                errors.append(
                    "MEMORY_STORES_CONFIG_PATH is required when MEMORY_ENABLED=true"
                )
            if not cls.MEMORY_DEFAULT_STORE_ID:
                errors.append(
                    "MEMORY_DEFAULT_STORE_ID is required when MEMORY_ENABLED=true"
                )
        if errors:
            raise ValueError(f"Config errors: {'; '.join(errors)}")


config = BoBClawConfig()

# ── Fan-out constants (module-level, not env-backed) ──────────────────────────
_FANOUT_THRESHOLD: int = 5  # Min subtask count to trigger Send-based fan-out
                            # Separate from _BULK_DISPATCH_THRESHOLD; do not consolidate.

# -- Hierarchical-managers (2-level agent tree) --
# manager_dispatch splits N subtasks into K sections; each section is one
# mini_manager (apex) that fans MANAGER_SECTION_SIZE workers. K = ceil(N/size),
# capped at MANAGER_MAX_SECTIONS (a per-turn `manager_max_sections` overrides).
MANAGER_SECTION_SIZE: int = 4   # target workers per mini-manager (STEERING: 4×4)
MANAGER_MAX_SECTIONS: int = 8   # ceiling on the number of mini-managers per turn

# -- Research orchestrator (MS2-R2) --
# Deterministic count->tier boundaries. RESEARCH_FANOUT_MIN is documentary (the
# single/fanout split is n<=1 vs n>=2); RESEARCH_HIER_THRESHOLD is the real cut.
RESEARCH_FANOUT_MIN: int = 2      # >= this (and < HIER) -> flat fan-out
RESEARCH_HIER_THRESHOLD: int = 10  # >= this -> hierarchical-managers

# -- Research subagent / IterResearch (MS2-R3) --
# The §2.5 condensed-return firewall + IterResearch round budget (DECISIONS-MS2 OD#3,
# SES-tuned later in R7 — sane config-tunable defaults here, NOT pre-committed thresholds).
RESEARCH_RETURN_TOKEN_CEILING: int = int(os.getenv("RESEARCH_RETURN_TOKEN_CEILING", "2000"))  # ≤2k condensed return
RESEARCH_MAX_ROUNDS: int = int(os.getenv("RESEARCH_MAX_ROUNDS", "3"))                          # IterResearch round budget
RESEARCH_MAX_CLAIMS: int = int(os.getenv("RESEARCH_MAX_CLAIMS", "8"))                          # cap on claims[] in the return
RESEARCH_MAX_SOURCES: int = int(os.getenv("RESEARCH_MAX_SOURCES", "8"))                        # cap on sources[] in the return

# -- Research lane: federated LKS read (hermes wiring) --
# Comma-separated federation instance ids the LIVE research lane reads LKS-first
# (e.g. "hermes"). Default empty ⇒ today's behavior byte-identical (no retriever,
# no research_subagent Sends). This is the research lane's OWN seam — it must NOT
# reuse MEMORY_LKS_FIRST/MEMORY_LKS_INSTANCE (the recall seam has REPLACEMENT
# semantics over BoB's own memory facts; pointing it at a foreign corpus is wrong).


def parse_research_lks_instances(raw: str) -> tuple[str, ...]:
    """Strict parse of a comma-separated instance-id list (the RESEARCH_LKS_INSTANCES env).

    Whitespace around ids is stripped; empty segments (``""``, ``"a,,b"``, ``" , "``) are
    dropped rather than passed on — ``ResearchRetriever`` rejects empty instance ids at
    construction, and a config-typo'd empty segment must not kill every research turn.
    Order is preserved (LKS tiers are consulted in listed order); duplicates are collapsed
    to the first occurrence so one instance is never read twice per query.
    """
    seen: dict[str, None] = {}
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen[name] = None
    return tuple(seen)


RESEARCH_LKS_INSTANCES: tuple[str, ...] = parse_research_lks_instances(
    os.getenv("RESEARCH_LKS_INSTANCES", "")
)

# -- Research convergence: refute-and-vote termination (MS2-R5) --
# Deterministic surviving-claim-set stability (the debate_converge Idea-ID no-delta pattern, keyed by bid_key)
# OR max_rounds / max_usd bind (DECISIONS-MS2 OD#5; LLM-chair is v2). The budget ceiling mirrors the council
# debate bound (COUNCIL_MAX_USD / DEBATE_ROUND_USD). SES-tuned later (R7); sane config-tunable defaults here.
RESEARCH_CONVERGE_MAX_ROUNDS: int = int(os.getenv("RESEARCH_CONVERGE_MAX_ROUNDS", "3"))        # refute-and-vote round cap
RESEARCH_CONVERGE_MAX_USD: float = float(os.getenv("RESEARCH_CONVERGE_MAX_USD", "0.50"))       # the MS-4 budget ceiling on rounds
RESEARCH_REFUTE_ROUND_USD: float = float(os.getenv("RESEARCH_REFUTE_ROUND_USD", "0.10"))       # per-round cost unit (cf. DEBATE_ROUND_USD)

# -- Fan-out timeout --
WORKER_TIMEOUT_SECONDS: int = 180   # asyncio.wait_for per worker (handoff 006)

# -- Critic (handoff 008) --
# LKS v3.1 rule 16: every worker output gated by a critic before join.
# Bounded action space (approve|flag|reject) + structured JSON output, so
# timeout is shorter than worker timeout.
CRITIC_TIMEOUT_SECONDS: int = 60

# -- Council synth-step timeout (MS9-W5, finding B) --
# The fusion/debate FINALIZATION synth call (``synthesize_node``) must be bounded like
# the per-seat worker call (``WORKER_TIMEOUT_SECONDS``). A synth backend that HANGS (no
# exception — e.g. an open socket that never responds) previously stalled the whole
# council forever: no completing ``council_synth`` / terminal ``council_event`` ever
# fired, so the app banner stuck on "Deliberating… $0.0000" (the live finding-B hang).
# On trip → the fallback chain advances; on total failure the node DEGRADES to the best
# answer so far AND emits a terminal frame (never hangs). Shorter than the worker cap
# (the synth is one bounded reconcile call, not an agentic worker turn).
COUNCIL_SYNTH_TIMEOUT_SECONDS: int = int(os.getenv("COUNCIL_SYNTH_TIMEOUT_SECONDS", "120"))

# -- Fan-out cost cap (handoff 007) --
# Per-backend pessimistic worst-case cost per worker. Pre-flight in
# dispatch_node sums these across planned workers; if total > remaining
# budget per _cost.remaining_budget, the whole turn aborts before fan-out fires.
#
# Values are placeholders at medium confidence. Tune via observation once
# bobclaw.core.fanout per-worker logs accumulate real usage data.
# Unmapped backends in the dispatch path raise a config error — no fallback.
MAX_WORKER_USD_BY_BACKEND: dict[str, float] = {
    "claude_api":     0.50,
    "claude_code":    0.00,  # subscription CLI (planning tier) — not metered per-call
    "agy_code":       0.00,  # agy subscription CLI (Gemini Second Voice) — not metered per-call
    "codex_code":     0.00,  # codex CLI via local LiteLLM proxy (glm/ds/qwen) — not metered per-call
    "kimi_platform":  0.10,
    "kimi_code":      0.05,
    "kimi_cli":       0.05,  # kimi CLI (membership) — flat-rate; $ cap is informational
    "pi_code":        0.05,  # pi agentic CLI (GLM/DeepSeek/MiniMax) — GLM flat, DS/MiniMax metered; pessimistic
    "opencode_serve": 0.00,
    "local":          0.00,
    "deepseek_v4_flash": 0.005,
    "deepseek_v4":    0.02,  # DS Pro — senior/audit tier (pricier than flash)
    "glm_5_2":        0.01,
    "minimax":        0.05,
    "gemini_flash": 0.02,
    "gemini_pro": 0.10,
    "gemini_deep_research": 0.10,
}

# Known backends — derived from MAX_WORKER_USD_BY_BACKEND so it stays in
# sync with the cost map.  "claude_managed" is not present because it's
# a face-only routing alias resolved to claude_api before fan-out sees it.
KNOWN_BACKENDS: frozenset[str] = frozenset(MAX_WORKER_USD_BY_BACKEND.keys())

# -- Fan-out width cap (handoff 007) --
# Per-backend max parallel workers. Kimi is the only true per-account hard
# cap (Allegretto = 30 shared with CLI/VS Code/Cowork; we leave 20 headroom).
# Claude is rate-limit-bounded, not spawn-bounded — soft cap with
# _pin_escalation as the actual backstop.
MAX_FANOUT_WIDTH_BY_BACKEND: dict[str, int] = {
    "kimi_code":     10,
    "kimi_cli":      10,  # shares the Allegretto 30-concurrent/account hard cap
    "kimi_platform": 10,
    "claude_api":    20,
    "claude_code":    1,  # planning tier — single heavy spawn, never a fan-out worker
    "agy_code":       8,  # planner is single-spawn but worker-agy IS a fan-out worker
    "codex_code":     8,  # worker-codex IS a fan-out worker (GPT-native via `-p gpt`)
    "pi_code":        8,  # agentic worker harness. NOTE: GLM-via-pi rides the Z.AI coding
                          # plan's serial-1 ceiling — a GLM roster must keep pi_code at width 1
                          # (per-provider cap is a roster concern; DeepSeek/MiniMax can fan wider).
    "opencode_serve": 1,
    "local":          1,
    "deepseek_v4_flash": 20,
    "deepseek_v4":   10,  # DS Pro — senior/audit tier, not fanned as wide as flash
    "glm_5_2":        1,  # SINGLE-LANE-ONLY ([[glm-single-lane-only]]): Z.AI coding plan caps
                          # concurrency ~1; combined with the hard cap below, any GLM fan-out
                          # fails LOUD instead of silently 429-ing. GLM = single serial calls only.
    "minimax":        10,
    "gemini_flash": 20,
    "gemini_pro": 10,
    "gemini_deep_research": 10,
}

# Absolute ceiling regardless of backend. No silent truncation, no chunking past this.
MAX_FANOUT_WIDTH_GLOBAL: int = 100

# Backends with a TRUE per-account/instance concurrency ceiling (as opposed to merely
# rate-limit-bounded). The SINGLE-WAVE build fan-out (Feature 2) fails LOUD rather
# than silently overrun when a build would exceed one of these backends'
# MAX_FANOUT_WIDTH_BY_BACKEND cap; spawn-unbounded fleets (deepseek / claude — the
# centerpiece demo proved 100 concurrent DeepSeek with no 429) are bounded only by
# MAX_FANOUT_WIDTH_GLOBAL. Until multi-wave build chunking lands. Kimi = 30 concurrent/
# account; `local` (one in-process model server) and `opencode_serve` (one workspace
# instance) are genuine single-instance ceilings (cap 1) — without them here a no-team
# build would fan N concurrent workers at a width-1 backend and overrun it.
HARD_CONCURRENCY_CAP_BACKENDS: frozenset[str] = frozenset(
    {"kimi_code", "kimi_cli", "kimi_platform", "local", "opencode_serve", "agy_code",
     "codex_code", "pi_code",
     # glm_5_2: Z.AI coding plan caps concurrency ~1 ([[glm-single-lane-only]]). Cap 1 + hard
     # ⇒ any GLM fan-out fails LOUD (rosters keep GLM single-lane; the FU1 cross-flight mutex
     # then gives true serial-1). Removed from fan-out critic/worker spots in core/teams.py.
     "glm_5_2"}
)

# Per-backend env override pattern: BOBCLAW_MAX_FANOUT_WIDTH_<BACKEND>
# e.g., BOBCLAW_MAX_FANOUT_WIDTH_KIMI_CODE=8
def _load_width_overrides() -> dict[str, int]:
    """Read BOBCLAW_MAX_FANOUT_WIDTH_<BACKEND> env vars and merge over the dict."""
    out = dict(MAX_FANOUT_WIDTH_BY_BACKEND)
    for backend in list(out.keys()):
        env_key = f"BOBCLAW_MAX_FANOUT_WIDTH_{backend.upper()}"
        val = os.getenv(env_key)
        if val is not None:
            try:
                out[backend] = int(val)
            except ValueError:
                raise ValueError(f"{env_key} must be an integer, got {val!r}")
    return out


# ── CoCouncil seat → backend map (P1; design §E "Seats: posture → backend") ───
# Vendor-decoupled seat dispatch: each posture maps to a default backend plus an
# ordered fallback chain (the Fable-pull lesson — never hard-bind a seat to one
# vendor). panel.py's resolve_seat_backend reads this; a profile override arg is
# accepted there but profiles YAML is P4, so P1b uses these table-E defaults.
#
# Postures (design table E):
#   framer    (voice 1) — frame, map constraints        → claude_code
#   stress    (voice 2) — structural assumption hunt    → agy_code
#   wildcard  (optional) — diversity seat               → deepseek_v4_flash
#   synth     (rotating) — reconcile + handoff [ROLE-01] → minimax
# (Chair is P3 — not wired in P1.) Every backend named here is a registered
# bobclaw-core backend in MAX_WORKER_USD_BY_BACKEND / execute._send_to_backend.
# Default deliberation shape for the `council-max` face. "fusion" = the validated
# parallel-panel pattern (seats answer blind → synth reconciles); "sequential" =
# the engine's native Claude→Gemini→synth chain in one node. Overridable per-request
# via a model_override shape hint. P2–P6 add debate/steered/grounding/budgets.
# Module-level (like the other COUNCIL_* below) so route._build_council_spec imports it.
COUNCIL_MODE_DEFAULT: str = os.getenv("COUNCIL_MODE_DEFAULT", "fusion")

# Seat defaults GENERALIZED from the my-bob prod policy (my-bob-next 9776dfc,
# 2026-07-18): the CLI-subscription tier leads per provider (claude_code /
# agy_code — flat-rate logins), metered/open providers fill behind; a failed
# CLI spawn (missing binary, throttle) walks the chain like any other seat
# error. Boxes retune per-deployment via the COUNCIL_SEAT_BACKENDS env var —
# JSON shaped {posture: {"backend": ..., "fallback_chain": [...]}} merged
# per-seat over these defaults — instead of patching this table downstream.
_COUNCIL_SEAT_DEFAULTS: dict[str, dict] = {
    "framer": {
        "backend": "claude_code",
        "fallback_chain": ["deepseek_v4_flash", "minimax"],
    },
    "stress": {
        "backend": "agy_code",
        "fallback_chain": ["gemini_flash", "deepseek_v4_flash"],
    },
    "wildcard": {
        "backend": "deepseek_v4_flash",
        "fallback_chain": ["kimi_code", "minimax"],
    },
    "synth": {
        "backend": "minimax",
        "fallback_chain": ["claude_code", "deepseek_v4_flash"],
    },
}


def _seat_backends_with_env_override(defaults: dict[str, dict], raw: str) -> dict[str, dict]:
    """Merge the COUNCIL_SEAT_BACKENDS env JSON over *defaults*, per seat.

    A seat entry overrides only the fields it names (backend and/or
    fallback_chain). Malformed JSON / a non-dict payload logs and returns the
    defaults untouched — a bad env var must not take the council down.
    """
    if not (raw or "").strip():
        return {k: dict(v) for k, v in defaults.items()}
    try:
        override = json.loads(raw)
    except json.JSONDecodeError:
        print("WARNING: COUNCIL_SEAT_BACKENDS env is not valid JSON; using defaults", file=sys.stderr)
        return {k: dict(v) for k, v in defaults.items()}
    if not isinstance(override, dict):
        print("WARNING: COUNCIL_SEAT_BACKENDS env must be a JSON object; using defaults", file=sys.stderr)
        return {k: dict(v) for k, v in defaults.items()}
    merged = {k: dict(v) for k, v in defaults.items()}
    for posture, entry in override.items():
        if not isinstance(entry, dict):
            continue
        seat = merged.setdefault(str(posture), {"backend": "", "fallback_chain": []})
        if entry.get("backend"):
            seat["backend"] = str(entry["backend"])
        if isinstance(entry.get("fallback_chain"), list):
            seat["fallback_chain"] = [str(b) for b in entry["fallback_chain"]]
    return merged


COUNCIL_SEAT_BACKENDS: dict[str, dict] = _seat_backends_with_env_override(
    _COUNCIL_SEAT_DEFAULTS, os.getenv("COUNCIL_SEAT_BACKENDS", "")
)

# Default fusion panel: the three core voices, blind in parallel. synth is the
# reconciler (not a panel seat) — it runs after the panel in synthesize_node.
COUNCIL_DEFAULT_SEATS: list[str] = ["framer", "stress", "wildcard"]
COUNCIL_DEFAULT_SYNTH_POSTURE: str = "synth"

# ── CoCouncil P2 — pre-close grounding gate + grounded restart + budgets ──────
# (Design §A2/A3/A4.) The grounding gate runs on the fusion close path: before
# synth converges, verify the answer's load-bearing factual claims against the
# live web (read-only) via claude_code + WebSearch; on web-detected drift do a
# grounded restart (re-seed round 1), bounded by a restart budget + a global
# cost ceiling. ADDITIVE: with COUNCIL_GROUND_CADENCE="off" (or non-council runs)
# the graph behaves exactly as P1 (synthesize → END), so the existing suite stays
# green. Grounding failures (parse/timeout/spawn) FAIL OPEN → converge.
#
# Cadence: "preclose" = verify once, right before convergence (LOCKED — not
# per-seat/per-round; early rounds are still forming, per-round web checks are
# wasted spend). "off" disables the gate entirely.
COUNCIL_GROUND_CADENCE: str = os.getenv("COUNCIL_GROUND_CADENCE", "preclose")
# Grounding tier: claude_code ONLY for P2 (connected, subscription, has WebSearch;
# the Gemini second-verifier is deferred). Drives which verifier the grounding
# node spawns — currently only "claude_code" is wired.
COUNCIL_GROUND_BACKEND: str = os.getenv("COUNCIL_GROUND_BACKEND", "claude_code")
# Restart budget (§A3): grounded restarts re-seed round 1 but burn a SEPARATE
# budget so drift→restart→drift→restart can't spin under the round cap.
COUNCIL_RESTART_BUDGET: int = int(os.getenv("COUNCIL_RESTART_BUDGET", "2"))
# Drift threshold (OPEN-B = ratio): restart iff the ratio of CONTRADICTED
# load-bearing claims to total load-bearing claims is >= this. `unverifiable`
# claims are flagged in the handoff but do NOT by themselves force a restart.
COUNCIL_DRIFT_THRESHOLD: float = float(os.getenv("COUNCIL_DRIFT_THRESHOLD", "0.34"))
# Global cost ceiling (§A3): hard runaway cutoff across panel + synth + grounding
# spawns for one council run. On breach → fail loud to the human with the best
# handoff so far. (Round budget / floor is P3's debate loop — config keys may be
# added there; P2 enforces only the restart budget + this global ceiling.)
COUNCIL_MAX_USD: float = float(os.getenv("COUNCIL_MAX_USD", "5.0"))
# ── CoCouncil P3 — debate loop (round-robin to convergence) ───────────────────
# Soft round cap for the `debate` shape: the deliberation loops round-robin until
# the handoff's [ACTIVE DEBATE] Idea-IDs converge (empty / no-delta), OR this many
# rounds elapse. A per-profile protocol_bounds.max_rounds overrides it. Distinct
# from COUNCIL_RESTART_BUDGET (grounded restarts) — debate uses `council_round`.
COUNCIL_MAX_ROUNDS: int = int(os.getenv("COUNCIL_MAX_ROUNDS", "3"))
# Per-debate-round cost estimate (USD) accumulated on council_cost_usd, checked
# against the cost ceiling before dispatching the next round (mirrors the grounding
# per-spawn estimate). Covers the round's N panel calls + the synth reconcile.
DEBATE_ROUND_USD: float = float(os.getenv("COUNCIL_DEBATE_ROUND_USD", "0.10"))
# Hard upper bound on a profile's protocol_bounds.max_rounds (rejected at author
# time). A debate round is ~4 graph super-steps, so this must keep
# 4*max_rounds + prologue under GRAPH_RECURSION_LIMIT (44 < 50 at the defaults) —
# fail the author loudly instead of crashing a paid run with GraphRecursionError.
COUNCIL_MAX_ROUNDS_CEILING: int = int(os.getenv("COUNCIL_MAX_ROUNDS_CEILING", "10"))
# Graph recursion limit (super-steps) for one chat turn. Must cover the deepest
# loop: a debate of up to COUNCIL_MAX_ROUNDS_CEILING rounds (~4 super-steps each) +
# the prologue, plus grounded restarts + fan-out waves. Default 50.
GRAPH_RECURSION_LIMIT: int = int(os.getenv("GRAPH_RECURSION_LIMIT", "50"))

# ── Build pipeline (Feature 2) — agentic plan→build→test→repair loop ──────────
# Per-turn build sandboxes live under this root. MUST be OUTSIDE the repo tree
# (like CC_SCRATCH_ROOT) so generated code + the pytest/CLI subprocess never touch
# tracked files. plan_contracts_node creates <root>/<conversation>/<stamp>-<rand>
# per turn; P3 hardens path-containment (permissions.evaluate_path) + the subprocess
# env. Opt-in / NOT in validate() (no build turn happens unless contracts are planned).
BUILD_WORKSPACE_ROOT: str = os.getenv("BUILD_WORKSPACE_ROOT", "C:/dev/scratch/bobclaw-build")
# Default contract count plan_contracts_node requests when a turn carries no
# build_units override. The live E2E (P4) drives a small N (10) through the graph.
BUILD_DEFAULT_UNITS: int = int(os.getenv("BUILD_DEFAULT_UNITS", "10"))
# Verify/repair loop (P2). BUILD_REPAIR_BUDGET = max repair passes (rounds) over a
# failing build before the loop converges fail-loud with the honest gate result
# (bounds the repair loop, like COUNCIL_RESTART_BUDGET bounds grounded restarts).
# 0 = no repair (verify once → END). BUILD_VERIFY_TIMEOUT = pytest subprocess wall
# clock. BUILD_REPAIR_UNIT_CAP bounds how many failing units one apex repair pass
# fixes (keeps the repair prompt sized; matches the demo's --repair-cap).
BUILD_REPAIR_BUDGET: int = int(os.getenv("BUILD_REPAIR_BUDGET", "1"))
BUILD_VERIFY_TIMEOUT: int = int(os.getenv("BUILD_VERIFY_TIMEOUT", "300"))
BUILD_REPAIR_UNIT_CAP: int = int(os.getenv("BUILD_REPAIR_UNIT_CAP", "25"))

# ── Build sandbox (P3.5) — Docker isolation for the verify gate ───────────────
# The verify gate EXECUTES LLM-written code. The P3 static gate + env-strip raise the
# bar but cannot contain Python; the REAL boundary is running the gate in a throwaway
# container with ONLY the per-turn workspace bind-mounted (no host secrets/repo),
# --network none, and resource caps. Modes:
#   "docker"     — force the container; FAIL-LOUD if the daemon/image is unavailable.
#   "subprocess" — host execution (P3 static gate + env-strip only); trusted models / CI.
#   "auto"       — docker when the daemon + image are available, else subprocess + a
#                  loud warning. (Default.)
# plan_contracts' build-empty gate runs only deterministic STUBS (no LLM code) so it
# stays on the host regardless. Build the image once:
#   docker build -t bobclaw-build-sandbox:py313 -f docker/build-sandbox.Dockerfile docker
BUILD_SANDBOX: str = os.getenv("BUILD_SANDBOX", "auto")
BUILD_SANDBOX_IMAGE: str = os.getenv("BUILD_SANDBOX_IMAGE", "bobclaw-build-sandbox:py313")
BUILD_SANDBOX_MEMORY: str = os.getenv("BUILD_SANDBOX_MEMORY", "512m")
BUILD_SANDBOX_PIDS: int = int(os.getenv("BUILD_SANDBOX_PIDS", "256"))
BUILD_SANDBOX_CPUS: str = os.getenv("BUILD_SANDBOX_CPUS", "2")
