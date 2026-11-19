# Upgrade BoB

Upgrade between pinned release tags. Read the target CHANGELOG.md and retain a
tested backup of databases, lane files, credentials and application state before
changing code or dependencies. A source checkout rollback does not reverse SQL
migrations or restore data.

## 1. Windows desktop installer — recommended

1. Close BoB. Download the installer for the target release from
   [the release assets](https://github.com/tyoung515-svg/bob/releases) and run it over
   the version you have. If it shows a page headed **Already Installed**, choose **Do
   not uninstall**. Never uninstall first with "Delete the application data" ticked:
   that removes the folder that holds your runtime.
2. Open BoB. A card headed **BoB update available** shows the installed and the bundled
   runtime version. Press **Upgrade** once and keep the window open.
3. BoB saves a backup of the runtime, copies the new one in, stops its services, updates
   the files and the database, and starts again. Allow several minutes, and have free
   disk space of about twice the size of your runtime. When it says **BoB updated
   successfully**, press **Reconnect**.

Your lane, conversations, memory and sign-ins are kept. If the update fails, BoB puts
the previous runtime back and says so; copy the message before trying again.

For an extra copy of your own, export the stopped distribution first with
`wsl --export BoB <backup-file>` in PowerShell. Do not replace the runtime's code by
hand with a source checkout. The runtime update uses the same SQL journal as first
boot.

If the app is set to **Connect to a Bob server**, update the app the same way and
upgrade the server by its own route below, then reconnect.

## 2. Docker datastore installation

These instructions apply to AGENTS-SETUP.md's Ubuntu host core/gateway with the
root `docker-compose.yml`. They do not describe upgrading the full runtime image.

1. Record the current tag, compose project, volume names, configured ports and
   service environment. Preserve `COMPOSE_PROJECT_NAME`; changing it can select
   empty volumes. Inspect with `sudo docker volume ls` and the same compose
   env/project used during installation.
2. Stop core and gateway before taking a consistent backup:

   ```bash
   sudo systemctl stop bob-source-gateway bob-source-core
   ```

3. Back up Postgres and both compose volumes using your tested backup procedure;
   also preserve `/var/lib/bob-source`, `.secrets`, and the installed service units.
   Qdrant/Redis live under compose in this route. For a cold volume backup, stop
   the three datastore containers first, copy their persistent volumes, then
   restart them. Never remove volumes to perform an upgrade.
4. Check out the target tag in `/opt/bob-source`. For example:

   ```bash
   cd /opt/bob-source
   sudo git fetch --tags
   sudo git checkout v0.99.1
   sudo .venv/bin/python -m pip install -r bobclaw-core/requirements.txt \
     -r bobclaw-gateway/requirements.txt 'textual>=0.80,<2'
   sudo docker compose --env-file .secrets/bobclaw.env -f docker-compose.yml \
     up -d --wait --wait-timeout 180 postgres redis qdrant
   ```

   Review datastore version changes before starting an existing volume with a
   new image; changing a Postgres major version needs its own data migration.
   These requirements files contain version ranges, not fully pinned lockfiles.
5. Reconcile the environment using the table below, preserving existing secrets.
   Apply all SQL with the shared runner:

   ```bash
   sudo -u bob-source /opt/bob-source/.venv/bin/python \
     /opt/bob-source/scripts/source-lane.py migrate
   ```

   Run it again to confirm unchanged files are skipped. It applies `init.sql` and
   every sorted migration, recording filename/hash in `bob_runtime_migrations`.
   Do not manually insert journal rows or rewrite an applied SQL file.
6. Start and verify:

   ```bash
   sudo systemctl start bob-source-core bob-source-gateway
   curl --fail http://127.0.0.1:7835/health
   curl --fail http://127.0.0.1:7836/health
   sudo -u bob-source /opt/bob-source/.venv/bin/python \
     /opt/bob-source/scripts/source-lane.py login
   sudo -u bob-source /opt/bob-source/.venv/bin/python \
     /opt/bob-source/scripts/source-lane.py broker
   sudo -u bob-source /opt/bob-source/.venv/bin/python \
     /opt/bob-source/scripts/source-lane.py broker-check
   ```

   The login command may reuse/refresh a valid human session. To exercise a fresh
   password/TOTP login, move the private `.secrets/tui-token.json` aside and wait
   for the next TOTP interval before retrying. Reconnect the desktop client, or
   launch `bobclaw-tui` using AGENTS-SETUP.md. A chat smoke test requires an enrolled
   backend and is separate from sign-in.

## 3. Native Ubuntu installation from source

Use the same stop, backup, tag checkout, dependency refresh, environment review,
migration and startup sequence as route 2. Replace compose operations with native
`postgresql`, `redis-server` and `bob-source-qdrant` service operations. Back up
Postgres, `/var/lib/bob-source/qdrant` and all lane/private state consistently;
stop Qdrant during a cold storage backup. Keep Qdrant's binary/config pinned to
the version reviewed for the target release. Follow its data-version upgrade
requirements before changing binaries against existing storage.

The install script deliberately refuses existing state. Do not rerun it
as an upgrade tool or erase lane state to bypass its guard. On a disposable clean
Ubuntu 24.04 amd64/systemd machine, try the target release with
`sudo bash scripts/install-from-source.sh`, or add `--docker` for compose datastores.
These commands install and leave the target services running.

### Configuration when moving from older host-process installs

Every required setting and its example is in AGENTS-SETUP.md and
`.secrets/bobclaw.env.example`. In particular:

| Setting / behavior | Required action |
| --- | --- |
| `BOBCLAW_PASSWORD` | Preserve/set the real plaintext password read by gateway auth; `BOBCLAW_PASSWORD_HASH` is not read. Keep a private base32 `TOTP_SECRET`. |
| `BOBCLAW_SECRET`, `JWT_SECRET` | Keep existing strong signing secrets; rotating them invalidates sessions. |
| `POSTGRES_PASSWORD`, `POSTGRES_URL` | Use the existing database password, with matching URL credentials. Compose now refuses an unset password. Changing the env does not rotate a password inside an existing Postgres volume. |
| `REDIS_URL`, `MEMORY_QDRANT_URL` | Explicit loopback URLs; Qdrant defaults here are 6353/6354. |
| `BOB_FAMILY_ROOT`, `BOB_DEFAULT_OWNER`, `BOB_MEMORY_OWNERS` | Set explicitly; standalone login subject remains `admin`. Do not silently rename existing data owners. |
| `BOB_LANE_ROOT` | Set an absolute private lane root; move data only with a reviewed backup/restore plan. |
| `BOB_ROSTER_FILE` | Set an explicit valid roster for roster mode, e.g. `/opt/bob-source/bobclaw-core/config/roster.example.yaml`, or leave completely unset for configured-lane mode. An empty selector fails. |
| `BOB_LANE_BACKENDS`, `BOB_LANE_DEFAULT_BACKEND` | Configure the lane's `lane.env`/`providers.env` and its actual provider credentials/models. The example roster enrolls no providers. |
| Core/gateway host, port and URLs | Use explicit loopback hosts, 7835/7836 and matching `CORE_URL`/`BOBCLAW_GATEWAY_URL`, or preserve another consistent pair. |
| `GATEWAY_SQLITE_PATH`, `MEMORY_SQLITE_PATH` | Preserve existing files and use absolute paths. Preserve refresh-token/replay state. |
| Memory flags | Set intentionally; no launcher should force memory/extraction on. Existing memory installations must preserve store/slot configuration and embedding dimensions. |
| `BUILD_SANDBOX` | Set `docker` on standalone hosts and all non-Linux hosts. `worker` requires the runtime image's Linux isolation and service provisioning. |
| `BOB_BROKER_TOKEN_FILE` | Select a private 0600 portal bearer file, or pass `--token-file`; there is no built-in token path. Preserve the lane subject/scope when rotating. |

For installations predating the standalone helper, do not run its `create` action
over old state. Export the existing service environment and run
`python3 scripts/migrate.py` with `psql` installed, then adapt launchers to the new
entry points (`bobclaw-core/start.py`, `bobclaw-gateway/gateway.py`). Review old
layout/ownership differences explicitly. The migration journal can adopt an old
schema only when the existing SQL is compatible; take a backup first and stop
on any error rather than marking it applied.

The old Kotlin desktop command is obsolete. Build `portal/shell` and select
**Connect to a Bob server**, or use `bobclaw-tui`. The desktop needs SSH and a Python
broker launcher selecting the correct loopback gateway and private portal token.
See AGENTS-SETUP.md for the launcher, token renewal and source feature limitations.

### Rollback

Stop application services. Restore the previous code/dependency environment and a
matching consistent backup of Postgres, Qdrant and lane/private state, including
the migration journal and SQLite databases. Restore previous service configuration
before starting again. Checking out an older tag alone does not undo schema or
credential-state changes. Retain the failed upgrade state for diagnosis; do not
overwrite the only good backup.
