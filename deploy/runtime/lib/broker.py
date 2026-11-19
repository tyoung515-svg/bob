"""Run the stdio portal broker against one runtime lane's gateway."""
from __future__ import annotations

import argparse
import json
import os
import pwd

from common import SOURCE
from lane import PORTAL, validate_name


def broker_argv(name: str) -> list[str]:
    validate_name(name)
    port = json.loads((PORTAL / f'{name}.json').read_text())['gateway_port']
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid lane gateway port')
    return [str(SOURCE / '.venv/bin/python'), '-u',
            str(SOURCE / 'portal-broker/portal-broker.py'),
            '--gateway', f'http://127.0.0.1:{port}',
            '--token-file', str(PORTAL / f'{name}.token'), '--portal-approvals']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', type=validate_name)
    args = parser.parse_args()
    uid = pwd.getpwnam('bobapp').pw_uid
    if os.getuid() != uid or os.geteuid() != uid:
        raise ValueError('run this command as bobapp inside the runtime; root and bob are not allowed')
    argv = broker_argv(args.name)
    descriptor = json.loads((PORTAL / f'{args.name}.json').read_text())
    settings_id = descriptor.get('settings_id', '')
    if __import__('re').fullmatch('[0-9a-f]{12}', settings_id):
        os.environ['BOB_SETTINGS_ID'] = settings_id
    else:
        os.environ.pop('BOB_SETTINGS_ID', None)
    os.execv(argv[0], argv)
    return 0
