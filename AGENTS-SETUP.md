# Install BoB

Choose the Windows desktop installer (recommended), Docker for the server
datastores, or an Ubuntu server built from source. Docker and source installs end
at password-and-TOTP sign-in through the gateway. There is no browser UI. The
front doors are the terminal client and the desktop app.

## 1. Windows desktop installer — recommended

You need 64-bit Windows 10 (version 2004 or later) or Windows 11, about 20 GB of free
disk space, an administrator account for the install, and an authenticator app on
your phone.

1. Download `BoB_<version>_x64-setup.exe` from
   [the release assets](https://github.com/tyoung515-svg/bob/releases) and run it. The
   installer is not signed yet, so Windows shows "Windows protected your PC"; choose
   **More info**, then **Run anyway**.
2. Open BoB and choose **Set up Bob on this PC**. The app checks the runtime bundle
   that came with the installer against its recorded hash, then imports it as its own
   WSL 2 distribution, named `BoB`. If WSL is not installed, setup shows the steps to
   add it; that needs one restart. Docker Desktop is not required.
3. Create your lane: a name and a password of at least 10 characters. Scan the QR code
   with your authenticator app.
4. Optionally install the local embedder, which lets BoB find memories by meaning.
5. Connect a model under **Settings → Keys & sign-ins**. Signing in to your lane does
   not connect a model; chat needs one.

The runtime sets up Postgres, Redis, Qdrant, the database schema, and for each lane a
core, a gateway, a bridge, workers and a broker. Lane ports start at 7835 (core), 7836
(gateway) and 8902 (bridge); further lanes get their own. Everything listens on
loopback inside the distribution.

To use the desktop app with a BoB server on another machine, choose **Connect to a Bob
server** instead and see "Front doors for Docker and source installs" below.
`install-bob.ps1`, the earlier host-process setup, is retired: it points to the
installer and exits.

## 2. Docker datastores with core and gateway from source

Use a fresh Ubuntu 24.04 **amd64** host with systemd and internet access for Ubuntu
packages, Python dependencies and container images. Under WSL, enable systemd in
`/etc/wsl.conf` (`[boot]` followed by `systemd=true`) and restart that distro first.
Run these commands inside Ubuntu, not Windows PowerShell.

Place a clean release checkout at `/opt/bob-source`, readable by other local users:

```bash
sudo git clone https://github.com/tyoung515-svg/bob.git /opt/bob-source
cd /opt/bob-source
sudo git checkout v0.99.1
sudo bash scripts/install-from-source.sh --docker
```

For an existing clean release tree there, run only the last command. This command
installs and verifies: it installs packages, creates private credentials and a
service account, provisions a lane, and leaves services running and enabled. Use a
fresh throwaway distro for verification. It refuses an existing source lane or
image worker installation; use UPGRADE.md for existing installations. Failure can
leave partial state: fix the failed step manually or use a fresh throwaway distro.

It installs Ubuntu's `docker.io`, `docker-compose-v2`, Python 3.12, venv/build
dependencies, the Postgres client, requirements from
`bobclaw-core/requirements.txt` and `bobclaw-gateway/requirements.txt`, and Textual.
It creates the unprivileged `bob-source` account and private environment, then runs:

```bash
sudo docker compose --env-file .secrets/bobclaw.env -f docker-compose.yml \
  up -d --wait --wait-timeout 180 postgres redis qdrant
```

Compose runs Postgres 16, Redis 7 and Qdrant 1.18.0, publishing only on loopback.
`POSTGRES_PASSWORD` is required, with no fallback. Its init.sql mount alone is
insufficient: all migrations must follow. Core and gateway run as Ubuntu host
services, **not** inside this compose file.

## 3. Ubuntu server from source

Use the same fresh Ubuntu/systemd prerequisites and checkout as route 2:

```bash
cd /opt/bob-source
sudo bash scripts/install-from-source.sh
```

This installs native Postgres 16 and Redis, and downloads Qdrant 1.18.0 amd64 using
the runtime image's archive URL and SHA-256 pin. Qdrant runs as `bob-source` under
`bob-source-qdrant.service`, on loopback 6353/6354. Postgres is restricted to
loopback and gets a random password. Redis uses Ubuntu's loopback configuration.
Lane creation, migrations, core/gateway and sign-in match route 2. No container
engine is needed for first sign-in in this mode.

### Installation and verification sequence

Both modes print one numbered PASS line per completed step, or a numbered FAIL
line and a nonzero exit on the first failure. They never print passwords, tokens
or one-time codes. Command output goes to `/var/log/bob-install-from-source.log`,
which only root can read; look there when a step fails, and review it before sharing.

| Step | Check/action |
| --- | --- |
| 1 | Root, Ubuntu 24.04 amd64, systemd, fresh state and safe checkout location. |
| 2 | Install OS/Python dependencies and a dedicated service account. |
| 3 | Create the private lane and random login/database/TOTP secrets. |
| 4 | Start datastores; authenticate to Postgres, ping Redis, check Qdrant health. |
| 5 | Apply init.sql and every sorted migrations/*.sql file. |
| 6 | Run migrations again; compare every journal filename/hash with the tree. |
| 7 | Start core and wait for loopback health. |
| 8 | Start gateway and wait for loopback health. |
| 9 | Sign in with password/TOTP; validate the signed owner and authenticated reads. |
| 10 | Create a private portal bearer; check stdio broker hello through the gateway. |

The migration command for either datastore route is:

```bash
sudo -u bob-source /opt/bob-source/.venv/bin/python \
  /opt/bob-source/scripts/source-lane.py migrate
```

For another deployment with `POSTGRES_URL` already exported, use
`python3 scripts/migrate.py` on Linux with `psql` installed, connecting as the
`bobclaw` application role. Do not put credentials
in arguments or shell history. It calls the runtime's
`deploy/runtime/lib/firstboot.py:apply_migrations`, using the same
`bob_runtime_migrations` filename/SHA-256 journal and per-file transactions.
`init.sql` runs first, then every SQL file in filename order (23 migrations in this
tree). Repeating the command skips unchanged files and rejects edited applied
files. Stop application services first and run only one migration command at a
time. Never edit applied SQL or manually mark migrations complete.

### Environment and lane settings

Installation creates `.secrets/bobclaw.env` and the lane's `lane.env` with mode
0600, owned by `bob-source`. Its home/state directory and `.secrets` are private.
Keep credentials and backups out of git. See `.secrets/bobclaw.env.example` for
operators configuring their own launchers.

Standalone password login retains the existing **admin** subject. This route
creates one lane at `/var/lib/bob-source/family/admin/bob-memory`, owned by `admin`.
Changing only an owner variable does not create another login. Use the runtime
image for isolated named multi-user lanes. Authentication is unchanged.

| Settings | Standalone example / meaning |
| --- | --- |
| `BOB_FAMILY_ROOT` | `/var/lib/bob-source/family` |
| `BOB_DEFAULT_OWNER`, `BOB_MEMORY_OWNERS` | `admin`, `admin` |
| `BOB_LANE_ROOT` | `/var/lib/bob-source/family/admin/bob-memory` |
| `BOB_ROSTER_FILE` | **Unset** for configured-lane mode. Alternative explicit roster: `/opt/bob-source/bobclaw-core/config/roster.example.yaml` (generic policy, no enrolled seats). Never set it empty. |
| `BOB_LANE_BACKENDS`, `BOB_LANE_DEFAULT_BACKEND` | `local`, `local` initially; install/configure local inference separately before chat. |
| `OLLAMA_URL`, `PREFERRED_LOCAL_MODEL` | `http://127.0.0.1:11434`, an installed chat-model ID (initially empty). |
| `BOBCLAW_PASSWORD`, `TOTP_SECRET` | Random plaintext password and base32 TOTP seed. Gateway does not read `BOBCLAW_PASSWORD_HASH`. |
| `BOBCLAW_SECRET`, `JWT_SECRET` | Independent random strings of at least 32 characters. Core/gateway share `BOBCLAW_SECRET`. |
| `POSTGRES_PASSWORD`, `POSTGRES_URL` | Random URL-safe password; `postgresql://bobclaw:<same-password>@127.0.0.1:5432/bobclaw`. |
| `REDIS_URL`, `MEMORY_QDRANT_URL` | `redis://127.0.0.1:6379/0`, `http://127.0.0.1:6353`. |
| `BOBCLAW_CORE_HOST`, `BOBCLAW_CORE_PORT` | `127.0.0.1`, `7835`. |
| `BOBCLAW_GATEWAY_HOST`, `BOBCLAW_GATEWAY_PORT` | `127.0.0.1`, `7836`. |
| `CORE_URL`, `BOBCLAW_GATEWAY_URL` | `http://127.0.0.1:7835`, `http://127.0.0.1:7836`. |
| `GATEWAY_SQLITE_PATH`, `MEMORY_SQLITE_PATH` | Absolute `gateway.db`, `bobclaw_memory.db` paths inside the lane root. Core's cache is also there via its working directory. |
| `MEMORY_ENABLED`, `MEMORY_L1_EXTRACTION_ENABLED` | `false`, `false`; first sign-in needs no embedder/extractor. |
| `BUILD_SANDBOX`, `BUILD_WORKSPACE_ROOT` | `docker`, `/var/lib/bob-source/family/admin/bob-memory/scratch/build`. Docker is the sandbox for the build-and-verify pipeline; install it to use that. Set `docker` on non-Linux hosts too. The desktop app's Builder needs the runtime image's workers and is not available on this route. |
| `BOB_OWNER_WORK_APPROVAL` | `signed_in_user`. |
| `BOB_BROKER_TOKEN_FILE` | `/var/lib/bob-source/family/admin/bob-memory/broker.token`; broker requires this or `--token-file`. |
| `COMPOSE_PROJECT_NAME` | `bobclaw`; preserve across upgrades. |
| `BOBCLAW_PG_PORT`, `BOBCLAW_REDIS_PORT`, `BOBCLAW_QDRANT_PORT`, `BOBCLAW_QDRANT_GRPC_PORT` | Compose host ports 5432, 6379, 6353, 6354. Match URLs when overriding. |

Configured-lane provider settings come from `lane.env` and `providers.env` inside
`BOB_LANE_ROOT`, not arbitrary provider keys in the process environment. For
example, a private `providers.env` owned by `bob-source` can contain
`BOB_LANE_BACKENDS=claude_api`, `BOB_LANE_DEFAULT_BACKEND=claude_api`,
`ANTHROPIC_API_KEY` and an explicitly entitled `ANTHROPIC_MODEL`. This optional paid
enrollment is not performed by installation. For local inference, configure the
lane's Ollama URL and installed model. Restart core/gateway after lane/provider
changes. The install script makes no model calls; sign-in does not prove chat readiness.

### Front doors for Docker and source installs

The terminal client is `bobclaw-tui`. After either route succeeds:

```bash
cd /opt/bob-source
sudo -u bob-source env PYTHONPATH=/opt/bob-source/bobclaw-tui \
  /opt/bob-source/.venv/bin/python -m bobclaw_tui --gateway 127.0.0.1:7836
```

It reads the private password/TOTP and caches/refreshes the human session in
`.secrets/tui-token.json`; installation primes that cache. Chat needs provider setup.

Build the desktop app from `portal/shell` on the client with Node/npm, Rust and
Tauri 2 platform prerequisites. On Ubuntu these include `build-essential`,
`pkg-config`, `libwebkit2gtk-4.1-dev`, `libssl-dev`,
`libayatana-appindicator3-dev`, `librsvg2-dev` and `patchelf`. Use a current Node LTS
and Rust toolchain:

```bash
cd portal/shell
npm ci
npm run tauri -- build
```

Open the resulting app and choose **Connect to a Bob server**. Enter host
`example-server`, the SSH user and absolute broker Python launcher path. This uses
SSH, not an exposed HTTP gateway. Key authentication must work with BatchMode and
a trusted host key; verify the server fingerprint first. Install/configure
`openssh-server` and authorized keys on the server if needed. Keep BoB/datastores
on loopback.

Installation creates the launcher
`/var/lib/bob-source/family/admin/bob-memory/portal-broker.py`. It runs
`portal-broker/portal-broker.py` with the venv, gateway 7836 and explicit
`--token-file`. Use the launcher path in the app: its SSH command supplies no
token-file argument. The 0600 bearer is minted with the existing
`create_portal_token`, subject `admin`, scope restricted to the lane tree. It is a
portal bearer, separate from the TUI's human session.

Both launcher and token belong to `bob-source`. For optional desktop SSH access,
an administrator must explicitly enable that account's shell
(`sudo usermod -s /bin/bash bob-source`), configure its authorized key, and permit
public-key login in sshd. Its home is `/var/lib/bob-source`. This SSH account is
trusted with the entire standalone deployment; it is not a multi-user confinement
boundary. Never make tokens or env files world-readable. Rotate the expiring bearer:

```bash
sudo -u bob-source /opt/bob-source/.venv/bin/python \
  /opt/bob-source/scripts/source-lane.py broker
```

Reconnect the app afterward. Source broker connections support existing gateway
surfaces; image-only recovery, credential controls, isolated workers, subscription
enrollment and bridge-dependent PC features are not provisioned here and can
report unavailable. Use the packaged runtime under `deploy/runtime/` for full
lane/worker provisioning; see its README and Containerfile. The root compose file
provides only datastores.

### Operations

```bash
sudo systemctl status bob-source-core bob-source-gateway
sudo systemctl restart bob-source-core bob-source-gateway
sudo systemctl stop bob-source-gateway bob-source-core
```

Native Qdrant is `bob-source-qdrant`; native Postgres/Redis are `postgresql` and
`redis-server`. With Docker, retain the same compose env/project when managing
datastores. Preserve data volumes. See UPGRADE.md before updating an installation.
