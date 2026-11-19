"""Root-installed proxy-bound profile and exact project Landlock selection."""
import uuid
from pathlib import Path
from core.portal_resources import safe_open
from .contract import WorkerUnavailable

PROFILE = {'argv':['/opt/bob/.venv/bin/python','-I','-m','core.worker.builder_runner'],
           'network':True,'egress':'lane-proxy/1','adapter_revision':'builder-command/1','filesystem_min_abi':3}


def project_root(config, request):
    if request['workspace'].startswith('run_'):
        import re
        identifier=request['workspace'][4:]
        if not re.fullmatch('[0-9a-f]{32}',identifier) or request['cwd'] or not config.get('run_workspace_root'):
            raise WorkerUnavailable('builder check workspace boundary unavailable')
        with safe_open(Path(config['run_workspace_root']),(identifier,),directory=True) as (_,path,_):
            return path.resolve(strict=True)
    if request['workspace'] != 'projects':
        raise WorkerUnavailable('builder requires a project workspace')
    project = request['cwd']
    try:
        if str(uuid.UUID(project)) != project:
            raise ValueError()
        with safe_open(Path(config['workspaces']['projects']), (project,), directory=True) as (_, path, _):
            return path.resolve(strict=True)
    except (ValueError, OSError) as exc:
        raise WorkerUnavailable('builder project boundary unavailable') from exc
