"""Opt-in uncommitted candidate snapshot; same narrow shipping allowlist as build.sh."""
import pathlib
import shutil
import subprocess
import sys

root, destination = map(pathlib.Path, sys.argv[1:])
allowed = {'bobclaw-core', 'bobclaw-gateway', 'portal-broker', 'migrations', 'deploy'}
files = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
for raw in files.split(b'\0'):
    if not raw:
        continue
    relative = pathlib.PurePosixPath(raw.decode('utf-8'))
    if not (relative.parts[0] in allowed or str(relative) in {'init.sql', 'LICENSE', 'NOTICE', '.secrets/bobclaw.env.example'}):
        continue
    if ('tests' in relative.parts or '__pycache__' in relative.parts or
            (relative.parts[0] == 'bobclaw-core' and 'tasks' in relative.parts) or
            relative.name == 'BOB-LEDGER-REVIEW.md' or '.bak-' in relative.name):
        continue
    source = root.joinpath(*relative.parts)
    if not source.exists():
        continue
    if source.is_symlink():
        raise ValueError('shipping snapshot refuses symlinks')
    target = destination.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if source.read_bytes().startswith(b'#!') else 0o644)
