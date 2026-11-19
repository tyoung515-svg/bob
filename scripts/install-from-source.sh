#!/usr/bin/env bash
# Fresh Ubuntu 24.04 amd64 installation and first gateway sign-in.
set -Eeuo pipefail
set +x
umask 077
exec 3>&1
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
repo=$PWD
mode=source
if [[ ${1:-} == --docker && $# == 1 ]]; then
    mode=docker
elif [[ $# != 0 ]]; then
    printf '1 FAIL: use no arguments or --docker\n'
    exit 1
fi
step=1
reason='Ubuntu 24.04 amd64, root, systemd, fresh state, and accessible checkout required'
if [[ $EUID != 0 ]]; then
    printf '1 FAIL: run this as root, for example with sudo\n'
    exit 1
fi
# Command output goes to a log only root can read, never to the terminal.
log=/var/log/bob-install-from-source.log
: >"$log"
chmod 600 "$log"
trap 'status=$?; printf "%s FAIL: required check: %s (exit %s); details in %s (root only; review before sharing)\n" "$step" "$reason" "$status" "$log" >&3; exit "$status"' ERR
run_step() {
    step=$1
    reason=$2
    # Never expose raw apt, Python, SQL, HTTP or service output on the terminal.
    printf '== step %s: %s\n' "$step" "$reason" >>"$log"
    "$3" >>"$log" 2>&1
    printf '%s PASS: %s\n' "$step" "$reason"
}
preflight() {
    [[ $EUID == 0 ]]
    . /etc/os-release
    [[ $ID == ubuntu && $VERSION_ID == 24.04 ]]
    [[ $(dpkg --print-architecture) == amd64 ]]
    [[ -d /run/systemd/system ]]
    [[ $repo =~ ^/[A-Za-z0-9_./-]+$ ]]
    [[ ! -e .secrets/bobclaw.env && ! -e /var/lib/bob-source ]]
    [[ ! -L .secrets && ! -L .venv && ! -L /var/lib/bob-source ]]
    [[ ! -e /etc/bob/execution-boundary ]]
    [[ ! -e /etc/systemd/system/bob-source-core.service ]]
    [[ ! -e /etc/systemd/system/bob-source-gateway.service ]]
    # Dedicated account must be created here, never adopt an existing account.
    if getent passwd bob-source >/dev/null; then return 1; fi
}
dependencies() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv python3-dev build-essential libpq-dev \
        postgresql-client-16 curl ca-certificates git openssh-client iproute2
    if [[ $mode == docker ]]; then
        apt-get install -y docker.io docker-compose-v2
        systemctl enable --now docker
        docker compose version
    else
        apt-get install -y postgresql-16 redis-server
    fi
    python3 -m venv .venv
    .venv/bin/python -m pip install -r bobclaw-core/requirements.txt -r bobclaw-gateway/requirements.txt 'textual>=0.80,<2'
    # Code/dependencies are root-owned and readable; secrets remain private.
    chmod -R a+rX .venv
    useradd --system --user-group --create-home --home-dir /var/lib/bob-source --shell /usr/sbin/nologin bob-source
    chmod 700 /var/lib/bob-source
    runuser -u bob-source -- test -r "$repo/scripts/source-lane.py"
    runuser -u bob-source -- test -x "$repo/.venv/bin/python"
    .venv/bin/python - <<'PY'
import socket
for port in (5432, 6379, 6353, 6354, 7835, 7836):
    # Postgres/Redis may have been started automatically by apt.
    if port in (5432, 6379):
        continue
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise SystemExit(f'port {port} on 127.0.0.1 is already in use')
PY
}
lane() {
    .venv/bin/python scripts/source-lane.py create
    chown -R bob-source:bob-source /var/lib/bob-source
    chown bob-source:bob-source .secrets .secrets/bobclaw.env
    chmod 700 .secrets
}
wait_url() {
    local url=$1
    local attempt
    for attempt in {1..90}; do
        if curl --fail --silent --max-time 2 "$url" >/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}
datastores() {
    if [[ $mode == docker ]]; then
        # apt installs only the client in this mode; refuse collisions with a host DB.
        docker compose --env-file .secrets/bobclaw.env -f docker-compose.yml up -d --wait --wait-timeout 180 postgres redis qdrant
    else
        systemctl enable --now postgresql redis-server
        # Loopback only, even if a distribution config changes its defaults.
        runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET listen_addresses = '127.0.0.1'"
        systemctl restart postgresql
        # Password is sent over stdin; it never appears in argv or output.
        .venv/bin/python - <<'PY'
import subprocess
from dotenv import dotenv_values
values = dotenv_values('.secrets/bobclaw.env')
password = values['POSTGRES_PASSWORD']
sql = ("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bobclaw') THEN "
       "CREATE ROLE bobclaw LOGIN; END IF; END $$;\n"
       f"ALTER ROLE bobclaw PASSWORD '{password}';\n")
existing = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1',
                           '-c', "SELECT 1 FROM pg_roles WHERE rolname='bobclaw' UNION ALL "
                           "SELECT 1 FROM pg_database WHERE datname='bobclaw'"],
                          text=True, check=True, capture_output=True)
if existing.stdout.strip():
    raise SystemExit('refusing an existing application role or database')
subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-v', 'ON_ERROR_STOP=1'],
               input=sql, text=True, check=True, capture_output=True)
subprocess.run(['runuser', '-u', 'postgres', '--', 'createdb', '-O', 'bobclaw', 'bobclaw'],
               check=True, capture_output=True)
PY
        install -d -m 700 -o bob-source -g bob-source /var/lib/bob-source/qdrant
        curl --fail --location --silent --show-error --max-time 300 \
            https://github.com/qdrant/qdrant/releases/download/v1.18.0/qdrant-x86_64-unknown-linux-musl.tar.gz \
            -o /var/lib/bob-source/qdrant.tar.gz
        printf '%s  %s\n' 76c38c3859682776d2bef61bf6c23b593b7c06682578f7c9ff9eac79c87a4268 /var/lib/bob-source/qdrant.tar.gz | sha256sum -c -
        tar -xzf /var/lib/bob-source/qdrant.tar.gz -C /var/lib/bob-source/qdrant
        install -m 755 /var/lib/bob-source/qdrant/qdrant /usr/local/bin/bob-source-qdrant
        cat >/var/lib/bob-source/qdrant.yaml <<'YAML'
storage:
  storage_path: /var/lib/bob-source/qdrant/storage
  snapshots_path: /var/lib/bob-source/qdrant/snapshots
service:
  host: 127.0.0.1
  http_port: 6353
  grpc_port: 6354
telemetry_disabled: true
YAML
        chown bob-source:bob-source /var/lib/bob-source/qdrant.yaml
        cat >/etc/systemd/system/bob-source-qdrant.service <<'UNIT'
[Unit]
Description=Bob source Qdrant
After=network.target
[Service]
User=bob-source
Group=bob-source
UMask=0077
WorkingDirectory=/var/lib/bob-source/qdrant
ExecStart=/usr/local/bin/bob-source-qdrant --config-path /var/lib/bob-source/qdrant.yaml
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
        systemctl daemon-reload
        systemctl enable --now bob-source-qdrant
    fi
    wait_url http://127.0.0.1:6353/healthz
    .venv/bin/python - <<'PY'
import asyncio
import asyncpg
import redis
import subprocess
from dotenv import dotenv_values
async def check():
    db = await asyncpg.connect(dotenv_values('.secrets/bobclaw.env')['POSTGRES_URL'], timeout=15)
    try:
        assert await db.fetchval('SELECT current_user') == 'bobclaw'
    finally:
        await db.close()
asyncio.run(check())
assert redis.Redis(host='127.0.0.1', port=6379, socket_timeout=5).ping()
for line in subprocess.check_output(['ss', '-H', '-ltn'], text=True).splitlines():
    address, port = line.split()[3].rsplit(':', 1)
    if port in {'5432', '6379', '6353', '6354'}:
        assert address in {'127.0.0.1', '[::1]', '::1'}, 'datastore listener is not loopback'
PY
}
migrate() {
    runuser -u bob-source -- "$repo/.venv/bin/python" "$repo/scripts/source-lane.py" migrate
}
repeat_migrations() {
    migrate
    .venv/bin/python - <<'PY'
import asyncio
import hashlib
from pathlib import Path
import asyncpg
from dotenv import dotenv_values
async def check():
    db = await asyncpg.connect(dotenv_values('.secrets/bobclaw.env')['POSTGRES_URL'], timeout=15)
    try:
        rows = await db.fetch('SELECT filename, sha256 FROM bob_runtime_migrations')
        expected = {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [Path('init.sql'), *sorted(Path('migrations').glob('*.sql'))]}
        assert dict((r['filename'], r['sha256']) for r in rows) == expected
    finally:
        await db.close()
asyncio.run(check())
PY
}
start_service() {
    local service=$1
    cat >"/etc/systemd/system/bob-source-$service.service" <<UNIT
[Unit]
Description=Bob source $service
After=network.target
[Service]
User=bob-source
Group=bob-source
UMask=0077
WorkingDirectory=/var/lib/bob-source/family/admin/bob-memory
ExecStart=$repo/.venv/bin/python $repo/scripts/source-lane.py $service
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now "bob-source-$service"
}
core() { start_service core; wait_url http://127.0.0.1:7835/health; }
gateway() { start_service gateway; wait_url http://127.0.0.1:7836/health; }
signin() { runuser -u bob-source -- "$repo/.venv/bin/python" "$repo/scripts/source-lane.py" login; }
broker() {
    runuser -u bob-source -- "$repo/.venv/bin/python" "$repo/scripts/source-lane.py" broker
    runuser -u bob-source -- "$repo/.venv/bin/python" "$repo/scripts/source-lane.py" broker-check
}
run_step 1 'fresh Ubuntu 24.04 amd64 root/systemd environment and unused source lane' preflight
run_step 2 'OS packages, readable Python dependencies and dedicated service account' dependencies
run_step 3 'private single-owner lane, random password/TOTP and required environment' lane
run_step 4 'Postgres application login, Redis ping, Qdrant health and loopback listeners' datastores
run_step 5 'init.sql and every sorted SQL migration with the runtime journal' migrate
run_step 6 'second migration run and matching journal filenames/hashes' repeat_migrations
run_step 7 'core startup and loopback health on 7835' core
run_step 8 'gateway startup and loopback health on 7836' gateway
run_step 9 'password/TOTP sign-in, signed owner and authenticated gateway reads' signin
run_step 10 'private portal token and stdio broker hello through the gateway' broker
