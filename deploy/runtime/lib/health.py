"""JSON unit and lane HTTP status, with nonzero exit for unhealthy active lanes."""
from __future__ import annotations

import json
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from common import read_env, require_root
from lane import DATA, lanes, units


def unit_status(name: str) -> dict:
    result = subprocess.run(['systemctl', 'show', name, '--property=LoadState,ActiveState,SubState,Result,UnitFileState'],
                            capture_output=True, text=True)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return {'name': name, **fields, 'query_ok': result.returncode == 0}


def endpoint(port: str) -> dict:
    url = f'http://127.0.0.1:{int(port)}/health'
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
            body = response.read(1024 * 1024)
            return {'url': url, 'healthy': response.status == 200, 'status': response.status,
                    'body': json.loads(body)}
    except (OSError, URLError, HTTPError, ValueError) as exc:
        return {'url': url, 'healthy': False, 'error': str(exc)}


def embedder_status(unit: dict) -> dict:
    from embedder import selection, loaded_alias
    from pathlib import Path
    try:
        selected = selection()
    except (OSError, ValueError, KeyError):
        return dict(ready=False, setup_needed=True, status='failed', detail='Invalid embedder selection; check /etc/bob/embedder.env')
    model = DATA / 'models' / Path(selected['path']).name
    report = {'selected_model': selected['model'], 'embedding_dimension': selected['dimension'],
              'model_path': str(model), 'ready': False, 'setup_needed': False}
    if unit.get('ActiveState') == 'failed' or not unit.get('query_ok'):
        return {**report, 'status': 'failed', 'detail': 'embedder failed; inspect journalctl -u bob-embedder.service'}
    if not model.is_file():
        return {**report, 'status': 'setup-needed', 'setup_needed': True,
                'detail': f'embedder model not installed at {model}'}
    health = endpoint('58081')
    healthy = (health['healthy'] and isinstance(health.get('body'), dict)
               and health['body'].get('status') == 'ok')
    model_loaded = loaded_alias(selected) if healthy else False
    ready = unit.get('ActiveState') == 'active' and healthy and model_loaded
    if ready:
        return {**report, 'ready': True, 'status': 'ready', 'health': health, 'detail': 'embedder ready'}
    if healthy and not model_loaded:
        return {**report, 'status': 'failed', 'health': health,
                'detail': 'configured embedder alias is absent from /v1/models; reselect the model'}
    if unit.get('ActiveState') in ('active', 'activating', 'reloading'):
        # llama-server answers 503 while it loads the model; that is not a failure.
        return {**report, 'status': 'starting', 'health': health,
                'detail': 'embedder is starting (model loading)'}
    return {**report, 'status': 'failed', 'health': health,
            'detail': 'embedder not running; inspect journalctl -u bob-embedder.service'}


def main() -> int:
    require_root()
    report = {'units': [], 'lanes': {}, 'firstboot_complete': (DATA / '.firstboot-complete').is_file()}
    names = ['bob-firstboot.service', 'postgresql.service', 'postgresql@16-main.service',
             'redis-server.service', 'bob-qdrant.service', 'bob-embedder.service']
    healthy = report['firstboot_complete']
    for name, path in lanes():
        values = read_env(path)
        disabled = (path.parent / '.disabled').exists()
        names.extend(units(name))
        core = endpoint(values['BOBCLAW_CORE_PORT'])
        gateway = endpoint(values['BOBCLAW_GATEWAY_PORT'])
        report['lanes'][name] = {'retained': disabled, 'core': core, 'gateway': gateway}
        if not disabled:
            healthy = healthy and core['healthy'] and gateway['healthy']
    report['units'] = [unit_status(name) for name in names]
    loaded = subprocess.run(['systemctl', 'list-units', '--all', '--output=json', '--no-pager'],
                            capture_output=True, text=True)
    report['system_units'] = json.loads(loaded.stdout) if loaded.returncode == 0 else []
    report['failed_units'] = [row['unit'] for row in report['system_units'] if row.get('active') == 'failed']
    report['embedder'] = embedder_status(next(unit for unit in report['units'] if unit['name'] == 'bob-embedder.service'))
    # failed_units is reported for diagnosis; only Bob's own units (checked below) gate health,
    # so an unrelated failed unit in a user's distro cannot block setup.
    healthy = healthy and loaded.returncode == 0
    if report['embedder']['status'] == 'failed':
        healthy = False
    for unit in report['units']:
        if (unit['name'] == 'bob-firstboot.service' and report['firstboot_complete']
                and unit.get('ActiveState') == 'inactive' and unit.get('Result') == 'success'):
            # On later boots ConditionPathExists skips the already-completed oneshot.
            continue
        if (unit['name'] == 'bob-embedder.service' and unit.get('ActiveState') == 'inactive'
                and unit['query_ok'] and report['embedder']['status'] == 'setup-needed'):
            # Preserve boot-health semantics, but expose incomplete memory setup.
            continue
        if any(unit['name'] in units(name) and entry['retained'] for name, entry in report['lanes'].items()):
            continue
        healthy = healthy and unit.get('ActiveState') == 'active' and unit['query_ok']
    report['healthy'] = bool(healthy)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if healthy else 1
