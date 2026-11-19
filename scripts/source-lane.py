#!/usr/bin/env python3
"""Provision and operate one standalone lane without the image's worker services."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ENV_FILE = SOURCE / '.secrets/bobclaw.env'
STATE = Path('/var/lib/bob-source')
LANE = STATE / 'family/admin/bob-memory'


def private_write(path: Path, content: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(content)


def create() -> None:
    if ENV_FILE.exists() or LANE.exists():
        raise ValueError('existing source lane; use the upgrade guide instead')
    LANE.mkdir(parents=True, mode=0o700)
    ENV_FILE.parent.mkdir(exist_ok=True, mode=0o700)
    password = secrets.token_urlsafe(32)
    values = {
        'POSTGRES_PASSWORD': password,
        'POSTGRES_URL': f'postgresql://bobclaw:{password}@127.0.0.1:5432/bobclaw',
        'REDIS_URL': 'redis://127.0.0.1:6379/0',
        'MEMORY_QDRANT_URL': 'http://127.0.0.1:6353',
        'BOBCLAW_PASSWORD': secrets.token_urlsafe(24),
        'TOTP_SECRET': base64.b32encode(secrets.token_bytes(20)).decode(),
        'BOBCLAW_SECRET': secrets.token_urlsafe(32), 'JWT_SECRET': secrets.token_urlsafe(32),
        'BOB_FAMILY_ROOT': str(STATE / 'family'), 'BOB_DEFAULT_OWNER': 'admin',
        'BOB_MEMORY_OWNERS': 'admin', 'BOB_LANE_ROOT': str(LANE),
        'BOB_LANE_BACKENDS': 'local', 'BOB_LANE_DEFAULT_BACKEND': 'local',
        'OLLAMA_URL': 'http://127.0.0.1:11434',
        'PREFERRED_LOCAL_MODEL': '',
        'BOBCLAW_CORE_HOST': '127.0.0.1', 'BOBCLAW_CORE_PORT': '7835',
        'BOBCLAW_GATEWAY_HOST': '127.0.0.1', 'BOBCLAW_GATEWAY_PORT': '7836',
        'CORE_URL': 'http://127.0.0.1:7835', 'BOBCLAW_GATEWAY_URL': 'http://127.0.0.1:7836',
        'GATEWAY_SQLITE_PATH': str(LANE / 'gateway.db'),
        'MEMORY_SQLITE_PATH': str(LANE / 'bobclaw_memory.db'),
        'MEMORY_ENABLED': 'false', 'MEMORY_L1_EXTRACTION_ENABLED': 'false',
        'BUILD_SANDBOX': 'docker', 'BUILD_WORKSPACE_ROOT': str(LANE / 'scratch/build'),
        'BOB_BROKER_TOKEN_FILE': str(LANE / 'broker.token'),
        'BOB_OWNER_WORK_APPROVAL': 'signed_in_user',
        'COMPOSE_PROJECT_NAME': 'bobclaw',
    }
    payload = ''.join(f'{key}={shlex.quote(value)}\n' for key, value in values.items())
    private_write(ENV_FILE, payload)
    private_write(LANE / 'lane.env', payload)
    print('Created the private standalone admin lane; no credentials printed')


def load() -> dict[str, str]:
    from dotenv import dotenv_values
    values = dict(dotenv_values(ENV_FILE, interpolate=False))
    if not values or any(value is None for value in values.values()):
        raise ValueError('private environment file missing or invalid')
    # Never opt an image installation out of its credential/worker identity.
    if any(os.environ.get(key) for key in ('BOB_SETTINGS_ID', 'BOB_WORKER_ID', 'BOB_EXECUTION_BOUNDARY')):
        raise ValueError('standalone helper cannot run with a runtime worker identity')
    if Path('/etc/bob/execution-boundary').exists():
        raise ValueError('standalone helper cannot operate an image installation')
    os.environ.pop('BOB_ROSTER_FILE', None)
    os.environ.update(values)
    os.environ['PYTHONPATH'] = str(SOURCE / 'bobclaw-core')
    sys.path.insert(0, str(SOURCE / 'bobclaw-core'))
    return values


def login(values: dict[str, str]) -> None:
    sys.path.insert(0, str(SOURCE / 'bobclaw-tui'))
    from bobclaw_tui.auth import get_access_token
    import jwt
    cache = ENV_FILE.parent / 'tui-token.json'
    token = asyncio.run(get_access_token(values['BOBCLAW_GATEWAY_URL'],
                                        cache_path=cache, env_file=ENV_FILE))
    claims = jwt.decode(token, values['JWT_SECRET'], algorithms=['HS256'])
    if claims['sub'] != values['BOB_DEFAULT_OWNER']:
        raise ValueError('gateway login returned the wrong owner')
    for endpoint in ('/auth/status', '/conversations'):
        request = urllib.request.Request(values['BOBCLAW_GATEWAY_URL'] + endpoint,
                                         headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError('authenticated gateway read failed')
    print('Password and TOTP sign-in succeeded; owner and authenticated reads checked')


def broker(values: dict[str, str]) -> None:
    sys.path.insert(0, str(SOURCE / 'bobclaw-gateway'))
    from auth import create_portal_token
    token = create_portal_token(user_id=values['BOB_DEFAULT_OWNER'],
                                scope={'may_touch': [str(LANE) + '/**'], 'may_not_touch': []})
    path = Path(values['BOB_BROKER_TOKEN_FILE'])
    # Explicit rotation: replace privately rather than truncating an existing bearer.
    temporary = path.with_suffix('.new')
    private_write(temporary, token + '\n')
    os.replace(temporary, path)
    wrapper = LANE / 'portal-broker.py'
    command = [str(SOURCE / '.venv/bin/python'), '-u', str(SOURCE / 'portal-broker/portal-broker.py'),
               '--gateway', values['BOBCLAW_GATEWAY_URL'], '--token-file', str(path)]
    payload = ('import os, sys\n' + f'command = {command!r}\n' +
               'os.execv(command[0], command + sys.argv[1:])\n')
    if not wrapper.exists():
        private_write(wrapper, payload)
    print('Private portal bearer rotated and Python SSH launcher prepared')


def broker_check(values: dict[str, str]) -> None:
    result = subprocess.run([sys.executable, str(LANE / 'portal-broker.py')],
                            input='{"id":1,"op":"hello","args":{}}\n',
                            text=True, capture_output=True, timeout=90)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    hello = next((row for row in replies if row.get('id') == 1), {})
    if result.returncode or hello.get('ok') is not True:
        raise ValueError('portal broker hello failed')
    print('Portal broker reached the loopback gateway with its private token')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'core', 'gateway', 'migrate', 'login', 'broker', 'broker-check'])
    args = parser.parse_args()
    try:
        os.umask(0o077)
        if args.action == 'create':
            create()
            return 0
        values = load()
        if args.action == 'login':
            login(values)
        elif args.action == 'broker':
            broker(values)
        elif args.action == 'broker-check':
            broker_check(values)
        else:
            target = {'core': 'bobclaw-core/start.py', 'gateway': 'bobclaw-gateway/gateway.py',
                      'migrate': 'scripts/migrate.py'}[args.action]
            os.chdir(values['BOB_LANE_ROOT'])
            os.execv(sys.executable, [sys.executable, str(SOURCE / target)])
    except (Exception, SystemExit):
        print('Source lane operation failed; credential-bearing details withheld', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
