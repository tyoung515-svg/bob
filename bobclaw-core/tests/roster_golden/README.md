# Pre-extraction oracle

`core.json` and `gateway.json` were captured before any tracked refactor edit at
commit `73add668b57cff8a2efbcf1aeb7d3d00b2681fab`. `provenance.json` records the
empty tracked diff and source/golden SHA-256 values. BU20B neutralizes deployment
names in the public fixtures and goldens; source hashes still describe the historical
capture commit, while golden hashes must be regenerated from the edited bytes.
`--write` uses exclusive creation and refuses replacement.

Original capture commands, from backend root:

```powershell
.\.venv-win\Scripts\python.exe bobclaw-core\tests\roster_golden\capture.py core --write
.\.venv-win\Scripts\python.exe bobclaw-core\tests\roster_golden\capture.py gateway --write
```

The harness fixes fake environment/credentials, disables dotenv, stubs local
discovery, binaries and service status, and invokes handlers directly. Import-time
tokenization is stubbed to avoid downloading tiktoken data. DNS, socket connections
and subprocess spawning are denied during capture. Windows event-loop wakeup
sockets are initialized before that guard. No live service, inference, listener or
database is used by capture. Assertions are over canonical UTF-8 JSON; array order
is retained. `--check-registry-identity` additionally checks HTTP/routing/bridge
singleton identity and explicit injection (including an empty registry).

Coverage: face summaries and every full face, backend list/catalog, available
models, routing view, each built-in team's routing resolution, intent and capability
class decisions, bridge models, gateway service catalog and `/v1/models`, council
defaults and actual execute targets for OpenCode unavailable, Claude/agy/Codex
throttle, Kimi/Pi 429 and Pi error. Every execute case also checks explicit escalation.

Core and gateway equivalence tests run capture in fresh subprocesses, once with
the deployment selector and once unset. Loader/startup tests reject bad explicit
selectors. Additional tests change manifest policy and check real handler/execute
results; an environment fixture checks unchanged override/merge precedence.

On this Windows 3.13 restricted token, pytest's mode-0700 temp directories acquire
an ACL the sandbox cannot use. The optional `windows_tmp.py` pytest plugin keeps
inherited ACLs only for newly-created repository `.test-tmp`/`test-db` directories.
It changes no existing ACL or production path. Linux does not need this plugin.
Create the `.test-tmp` parent before selecting a fresh `--basetemp` child.

BU7: `conversation_faces.json` is a separate golden for the configured-lane Assistant/Planner names and declared/effective tool inventories. It is checked by `test_bu7_planner.py`; the pre-extraction `core.json`, `gateway.json` and their provenance remain unchanged because the explicit deployment roster is unchanged.

BU11: `conversation_faces.json` also records the Reviewer's declared tools, registered allowlist, effective ordinary chat tools, and offered tools in a workflow read stage. The three workspace readers are available only in that workflow context. The historical deployment captures and provenance remain unchanged.
