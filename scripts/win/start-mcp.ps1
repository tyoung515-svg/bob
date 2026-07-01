# BoBClaw MCP server (stdio) — publishes chat_with_face + run_council (Neck Beard MODE).
# An MCP client (Claude Code / Codex) launches this as a stdio server; it proxies to the
# gateway /ws/chat, so the gateway (and ideally core) must be up. Needs a scoped agent
# token minted via POST /auth/agent-token (admin-authed). Logs go to stderr; stdout is the
# MCP protocol channel.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found: $py" }
$env:PYTHONPATH = "$repo\bobclaw-core"
if (-not $env:BOBCLAW_AGENT_TOKEN) {
    throw "BOBCLAW_AGENT_TOKEN not set — mint one via POST /auth/agent-token (admin-authed) and export it."
}
if (-not $env:BOBCLAW_GATEWAY) { $env:BOBCLAW_GATEWAY = "http://127.0.0.1:7826" }
Set-Location "$repo\bobclaw-core"
& $py mcp_serve.py @args
