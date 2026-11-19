#!/usr/bin/env python3
"""Apply the runtime SQL journal using POSTGRES_URL (requires psql on PATH)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'deploy/runtime/lib'))
sys.path.insert(0, str(SOURCE / 'bobclaw-core'))
from firstboot import apply_migrations


def main() -> int:
    try:
        url = urlsplit(os.environ['POSTGRES_URL'])
        if url.scheme not in {'postgres', 'postgresql'} or not url.password:
            raise ValueError('POSTGRES_URL must include a database password')
        env = dict(os.environ)
        # libpq environment keeps the password out of process arguments.
        env.update(PGHOST=url.hostname or '127.0.0.1', PGPORT=str(url.port or 5432),
                   PGUSER=unquote(url.username or 'bobclaw'),
                   PGPASSWORD=unquote(url.password), PGDATABASE=unquote(url.path.lstrip('/')),
                   PGCONNECT_TIMEOUT='10')
        if url.query:
            raise ValueError('use libpq PGSSL* variables instead of URL query options')

        def execute(sql: str, database: str = 'bobclaw') -> str:
            result = subprocess.run(['psql', '-X', '-w', '-At', '--set=ON_ERROR_STOP=1'],
                                    input=sql, text=True, env=env, capture_output=True)
            if result.returncode:
                raise ValueError('database migration failed; SQL output withheld to protect credentials')
            return result.stdout.strip()

        apply_migrations(source=SOURCE, execute=execute)
    except (KeyError, OSError, ValueError) as exc:
        # A URL parser error can contain operator input. Expose only known messages.
        safe_messages = {
            'POSTGRES_URL must include a database password',
            'use libpq PGSSL* variables instead of URL query options',
            'database migration failed; SQL output withheld to protect credentials',
            'unsafe migration filename',
        }
        if str(exc) in safe_messages or str(exc).startswith('applied migration changed: '):
            print(str(exc), file=sys.stderr)
        else:
            print('migration failed; check POSTGRES_URL, psql and SQL files; details withheld', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
