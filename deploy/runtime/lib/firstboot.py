"""Initialize secrets and transactionally journal ordered SQL files."""
from __future__ import annotations

import fcntl
import hashlib
import os
import pwd
import re
import subprocess

from common import DATA, SOURCE, private_write, read_env, render_env, require_root


def psql(sql: str, database: str = 'postgres') -> str:
    # SQL (including passwords) travels on stdin, never in argv or environment.
    result = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-X',
                             '--set=ON_ERROR_STOP=1', '--no-psqlrc', '-At', '-d', database],
                            input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # psql error context may echo secret-bearing SQL. Do not forward it.
        raise ValueError('Postgres setup failed; SQL output withheld to protect credentials')
    return result.stdout.strip()


def apply_migrations(source=None, execute=None):
    """Shared firstboot/upgrade/standalone runner with one hashed SQL journal."""
    source = SOURCE if source is None else source
    execute = psql if execute is None else execute
    # Apply as the application role so later startup's init.sql has ownership privileges.
    execute('SET ROLE bobclaw; CREATE TABLE IF NOT EXISTS bob_runtime_migrations ('
            'filename text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());', 'bobclaw')
    files = [source / 'init.sql', *sorted((source / 'migrations').glob('*.sql'))]
    for path in files:
        name = path.relative_to(source).as_posix()
        if re.fullmatch(r'[A-Za-z0-9_./-]+', name) is None:
            raise ValueError('unsafe migration filename')
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        applied = execute(f"SELECT sha256 FROM bob_runtime_migrations WHERE filename='{name}';", 'bobclaw')
        if applied:
            if applied != digest:
                raise ValueError(f'applied migration changed: {name}')
            print(f'Skipped {name}')
            continue
        execute('BEGIN; SET LOCAL ROLE bobclaw;\n' + payload.decode() + '\n'
                f"INSERT INTO bob_runtime_migrations(filename, sha256) VALUES ('{name}', '{digest}');\nCOMMIT;", 'bobclaw')
        print(f'Applied {name}')


def main() -> int:
    require_root()
    os.umask(0o077)
    with open('/run/bob-firstboot.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = DATA / '.firstboot-complete'
        if marker.exists():
            print('Firstboot already complete')
            return 0
        from common import execution_mode
        account = pwd.getpwnam('root' if execution_mode() == 'worker' else 'bob')
        env_path = DATA / 'secrets' / 'bobclaw.env'
        if not env_path.exists():
            subprocess.run([str(SOURCE / '.venv/bin/python'), str(SOURCE / 'deploy/new-env.py'),
                            str(env_path)], check=True)
        # Also normalize an env left by an interrupted new-env invocation.
        values = read_env(env_path)
        values['POSTGRES_URL'] = f'postgresql://bobclaw:{values["POSTGRES_PASSWORD"]}@127.0.0.1:5432/bobclaw'
        values['REDIS_URL'] = 'redis://127.0.0.1:6379/0'
        # Authentication belongs to lanes; do not leave a shared placeholder login.
        for key in ('BOBCLAW_PASSWORD', 'TOTP_SECRET'):
            values.pop(key, None)
        temporary = env_path.with_suffix('.new')
        # This is a private root-controlled directory, guarded by the firstboot lock.
        temporary.unlink(missing_ok=True)
        try:
            private_write(temporary, render_env(values), account.pw_uid, account.pw_gid)
            os.replace(temporary, env_path)
        finally:
            temporary.unlink(missing_ok=True)
        os.chmod(env_path, 0o600)
        os.chown(env_path, account.pw_uid, account.pw_gid)
        values = read_env(env_path)
        password = values['POSTGRES_PASSWORD']
        # new-env generates URL-safe tokens; refuse malformed edits before constructing SQL.
        if re.fullmatch(r'[A-Za-z0-9_-]{32,}', password) is None:
            raise ValueError('POSTGRES_PASSWORD must be a new-env URL-safe token')
        psql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bobclaw') THEN "
             "CREATE ROLE bobclaw LOGIN; END IF; END $$;\n"
             f"ALTER ROLE bobclaw PASSWORD '{password}';\n")
        if psql("SELECT 1 FROM pg_database WHERE datname='bobclaw';") != '1':
            psql('CREATE DATABASE bobclaw OWNER bobclaw;')
        apply_migrations()
        private_write(marker, 'complete\n', account.pw_uid, account.pw_gid)
    print('Firstboot complete')
    return 0
