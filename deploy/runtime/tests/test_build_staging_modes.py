"""The source-only staging context must be readable under a restrictive umask."""
import os, shutil, stat, subprocess
from pathlib import Path
import pytest

RUNTIME = Path(__file__).resolve().parents[1]
FAKE_DOCKER = r'''#!/usr/bin/env bash
if [[ $1 == build ]]; then
    ctx=${@: -1}
    { stat -c '%a %n' "$ctx" "$ctx/deploy/runtime/Containerfile" "$ctx/deploy/runtime/lib/x.py" "$ctx/deploy/runtime/bin/run"; } > "$FAKE_RECORD"
    exit 7
fi
exit 0
'''


def _repo(tmp_path):
    repo = tmp_path / 'repo'
    for rel, mode in {'deploy/runtime/Containerfile': 0o644, 'deploy/runtime/lib/x.py': 0o644, 'deploy/runtime/bin/run': 0o755,
                      'bobclaw-core/a.py': 0o644, 'bobclaw-gateway/a.py': 0o644, 'portal-broker/a.py': 0o644,
                      'migrations/1.sql': 0o644, 'init.sql': 0o644, 'LICENSE': 0o644, 'NOTICE': 0o644,
                      '.secrets/bobclaw.env.example': 0o644}.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x\n')
        path.chmod(mode)
    shutil.copytree(RUNTIME, repo / 'deploy/runtime', dirs_exist_ok=True, ignore=shutil.ignore_patterns('tests', '__pycache__'))
    env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'}
    for cmd in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 'x']):
        subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env)
    return repo


@pytest.mark.parametrize('mask', ['077', '022'])
def test_archive_staging_is_world_readable_public_source_under_any_umask(tmp_path, mask):
    repo = _repo(tmp_path)
    bin_dir = tmp_path / 'fakebin'
    bin_dir.mkdir()
    (bin_dir / 'docker').write_text(FAKE_DOCKER)
    (bin_dir / 'docker').chmod(0o755)
    record = tmp_path / 'record'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FAKE_RECORD': str(record)}
    result = subprocess.run(['bash', '-c', f'umask {mask}; exec bash "$1" test-v', 'x', str(repo / 'deploy/runtime/build.sh')],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 7, result.stderr[-500:]
    modes = {line.split(' ', 1)[1].rsplit('/', 1)[-1]: int(line.split()[0], 8) for line in record.read_text().splitlines()}
    for name, mode in modes.items():
        assert mode & stat.S_IROTH, f'{name} staged unreadable: {oct(mode)}'
    assert modes['run'] & stat.S_IXOTH
    assert not any(m & stat.S_IWOTH for m in modes.values()), 'no world-writable staging'
