# Deployment helpers

For a new BoB 0.99.1 installation, use [AGENTS-SETUP.md](../AGENTS-SETUP.md).
The Windows installer uses the image in [runtime/](runtime/README.md).
The root Compose file provides datastores; it does not run core or gateway.

## Portable environment and user-unit helpers

Run these with the checkout's Python environment. Neither helper installs units
or starts services:

```bash
.venv/bin/python deploy/new-env.py /path/to/data/bobclaw.env
.venv/bin/python deploy/render-units.py \
  --bob-home /path/to/checkout \
  --env-file /path/to/data/bobclaw.env \
  --output-dir /path/to/staged-units
```

The environment generator starts from the example, generates new shared-signing,
Postgres and base32 TOTP secrets, updates the password in `POSTGRES_URL`, and
blanks provider credential fields. It creates the file exclusively with mode
0600 and refuses existing files, including symlinks. It does not set a real
`BOBCLAW_PASSWORD`, provision a lane, apply migrations or enroll a model.
Review the result against the install guide before starting services.

The renderer stages every `deploy/systemd/*.service.in` template. The supported
core/gateway templates default to 7835/7836, and the bridge points at core 7835;
environment overrides must keep server ports and client URLs consistent.
Review the rendered paths, environment and service dependencies before installing
any unit. These generic user units do not provision runtime identities, workers
or credential controls and must not replace the image's per-lane units.

The Discord front end is not part of this release, so there is no unit for it.

## Explicit memory paths

Operators adapting an existing deployment can select memory roots in its private
environment. These variables name filesystem memory surfaces, not extra sign-ins
or independently isolated runtime lanes:

```dotenv
BOB_FAMILY_ROOT=/path/to/data/personal
BOB_SPS_LANE_ROOT=/path/to/data/work
BOB_SHARED_LANE_ROOT=/path/to/data/shared/board
MEMORY_SQLITE_PATH=/path/to/data/memory/bobclaw_memory.db
```

`SPS_LANE_ROOT` is a legacy alias; `BOB_SPS_LANE_ROOT` takes precedence.
`BOB_DEFAULT_OWNER` selects the owner for personal-memory access.
Unconfigured personal, work and shared roots have no built-in filesystem path.
An unavailable root is reported rather than discovered from another deployment.
`BOB_MEMORY_OWNERS` is a comma-separated list used for legacy agent-memory
coverage; otherwise the configured default owner and `shared` are considered.
Token scopes must authorize the actual configured root; relocating data does not
translate existing scopes into new authority. Relative `MEMORY_SQLITE_PATH`
values resolve from the `bobclaw-core` source directory.

## Optional legacy SSH model routes

These routes are for explicitly configured standalone deployments, not the
packaged worker route. Configure only endpoints you intend to trust and restart
core after changing host configuration:

| Setting | Purpose |
| --- | --- |
| `BOB_CLAUDE_LINUX_HOST` | Primary Linux SSH alias |
| `BOB_CLAUDE_WINDOWS_HOST` | Primary Windows SSH alias |
| `BOB_CLAUDE_ALTERNATE_HOST` | Separately pinned Windows account |
| `BOB_CLAUDE_LINUX_SCRIPT` | Linux helper receiving the model slug |
| `BOB_CLAUDE_WINDOWS_SCRIPT` | Primary Windows helper |
| `BOB_CLAUDE_SONNET_WINDOWS_SCRIPT` | Sonnet Windows helper |
| `BOB_CLAUDE_HAIKU_WINDOWS_SCRIPT` | Haiku Windows helper |
| `BOB_CLAUDE_ALTERNATE_WINDOWS_SCRIPT` | Alternate helper receiving model and optional effort |
| `BOB_STRATA_LINUX_COMMAND` | Full remote local-model bridge command on Linux |
| `BOB_STRATA_WINDOWS_COMMAND` | Full remote local-model bridge command on Windows |

`BOB_CLAUDE_HOST=auto` probes configured primary hosts, Linux first.
`linux` or `windows` pins a platform. Missing host/helper configuration is
reported unavailable; it does not supply a host default or silently choose the
alternate account. The legacy local-model bridge uses the configured primary
hosts too.

`BOB_ROSTER_FILE` selects an explicit deployment roster.
`bobclaw-core/config/roster.example.yaml` is a generic example with no enrolled
providers. Leave the selector completely unset for configured-lane mode; an empty
or invalid selector is an error. Test fixtures are excluded from runtime images.

Read [SECURITY.md](../SECURITY.md) before changing listener or credential settings.
