#!/opt/bob/.venv/bin/python
"""Read-only PA2 probes; install beside acceptance.py in a disposable runtime."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import sys

MODEL = Path('/var/lib/bob/models/Qwen3-Embedding-4B-Q4_K_M.gguf')
PYTHON = '/opt/bob/.venv/bin/python'
sys.path.insert(0, '/opt/bob/bobclaw-core')
sys.path.insert(0, '/usr/local/lib/bob_runtime')


def command(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, cwd='/',
                          timeout=kwargs.pop('timeout', 30), **kwargs)


def vector_receipt(vectors, dimension=2560):
    dims = [len(vector) for vector in vectors]
    finite = all(math.isfinite(value) for vector in vectors for value in vector)
    norms = [math.sqrt(sum(value * value for value in vector)) for vector in vectors]
    valid = (len(vectors) == 2 and dims == [dimension, dimension] and finite
             and all(math.isfinite(norm) and norm > 1e-9 for norm in norms)
             and vectors[0] != vectors[1])
    return dict(valid=valid, count=len(vectors), dimensions=dims, finite=finite,
                norms=norms, distinct=len(vectors) == 2 and vectors[0] != vectors[1])


def child(lane):
    from common import read_env
    root = Path('/var/lib/bob/family') / lane / 'bob-memory'
    os.environ.clear()
    os.environ.update({'PATH': '/usr/local/bin:/usr/bin:/bin', 'BOB_SERVICE_ROLE': 'core'})
    os.environ.update(read_env(root / 'core.env'))
    from core.memory.embedder import SlotResolvedEmbedder
    from core.memory.slots import SlotResolver
    resolver = SlotResolver(Path(os.environ['MEMORY_SLOTS_FILE']))
    resolution = resolver.get('embed_text')
    # Fail closed before sending any request if the lane points outside the packaged service.
    if (resolution.endpoint != 'http://127.0.0.1:58081' or resolution.backend != 'lmstudio'
            or (resolution.model, resolution.embedding_dimension) not in {
                ('qwen3-embedding-4b', 2560), ('qwen3-embedding-0.6b', 1024)}):
        raise ValueError('lane embedding slot does not match packaged loopback embedder')

    async def embed():
        return await asyncio.wait_for(SlotResolvedEmbedder(resolver).embed([
            'A blue bicycle waits beside the garden gate.',
            'Runtime embeddings preserve semantic memory across conversations.',
        ]), timeout=120)

    print(json.dumps({**vector_receipt(asyncio.run(embed()), resolution.embedding_dimension), 'uid': os.getuid()}))


def run(lane, emit):
    """No restart/reset/provisioning before the first-boot failure observation."""
    failures = []

    def record(probe, passed, **evidence):
        emit(probe, passed, **evidence)
        if not passed:
            failures.append(probe)

    def skip(probe, reason):
        print(json.dumps(dict(probe=probe, passed=None, status='skipped', reason=reason)), flush=True)

    boot = command('systemctl', 'is-system-running', '--wait', timeout=180)
    failed = command('systemctl', '--failed', '--no-legend', '--plain', '--no-pager')
    record('runtime.failed-units', boot.stdout.strip() == 'running' and failed.returncode == 0
           and not failed.stdout.strip(), boot_state=boot.stdout.strip(),
           units=failed.stdout.strip(), error=failed.stderr.strip())
    record('runtime.kmod', os.access('/usr/bin/kmod', os.X_OK))
    backend = command('/usr/local/libexec/bob/check-embedder-backend')
    record('embedder.cpu-backend', backend.returncode == 0,
           output=backend.stdout[-4000:], error=backend.stderr[-1000:])
    result = command('bob-health')
    report = json.loads(result.stdout)
    status = report.get('embedder', {})
    # Old receipts default to the original model; new receipts carry the selection.
    model = Path(status.get('model_path', str(MODEL)))
    record('embedder.health-receipt', isinstance(report.get('healthy'), bool)
           and isinstance(report.get('lanes'), dict) and result.returncode == 0,
           healthy=report.get('healthy'), embedder=status)
    if not model.is_file():
        record('embedder.missing-model', status.get('status') == 'setup-needed'
               and status.get('setup_needed') is True and status.get('ready') is False
               and status.get('detail') == f'embedder model not installed at {model}', status=status)
    else:
        skip('embedder.missing-model', 'model present; run once on the fresh model-free runtime too')
    supplied = os.environ.get('BOB_ACCEPT_EMBED_MODEL')
    if not supplied:
        skip('embedder.lane-client', 'BOB_ACCEPT_EMBED_MODEL not supplied; no model installed or downloaded by this probe')
    else:
        path = Path(supplied)
        installed = path.is_file() and model.is_file() and path.samefile(model)
        record('embedder.supplied-model', installed, path=supplied,
               required_path=str(model))
        if installed:
            from health import endpoint
            health = endpoint('58081')
            record('embedder.http-health', health.get('healthy') is True
                   and isinstance(health.get('body'), dict) and health['body'].get('status') == 'ok', health=health)
            from common import read_env
            env = read_env(Path('/var/lib/bob/family') / lane / 'bob-memory/lane.env')
            cfg = json.loads((Path('/etc/bob/workers') / (env['BOB_WORKER_ID'] + '.json')).read_text())
            user = command('systemctl', 'show', f'bob-core@{lane}.service', '--property=User', '--value')
            identity = user.returncode == 0 and user.stdout.strip() == cfg['service_user'] and cfg['service_uid'] != 0
            record('embedder.lane-identity', identity, lane=lane, user=user.stdout.strip())
            if identity:
                client = command('runuser', '-u', cfg['service_user'], '--', PYTHON, '-I',
                                 str(Path(__file__).resolve()), '--child', lane, timeout=150)
                receipt = json.loads(client.stdout) if client.returncode == 0 else {}
                record('embedder.lane-client', client.returncode == 0 and receipt.get('valid') is True
                       and receipt.get('uid') == cfg['service_uid'], receipt=receipt,
                       error=client.stderr[-1500:])
    emit('overall', not failures, failures=failures)
    return int(bool(failures))


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--child':
        raise SystemExit('Run acceptance.py --embedder --lane-a <existing-lane> as root')
    from lane import validate_name
    child(validate_name(sys.argv[2]))
