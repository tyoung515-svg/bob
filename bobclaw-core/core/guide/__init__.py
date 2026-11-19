"""Small, read-only product guide. Topic discovery is file-based; no memory reads."""
from __future__ import annotations

from datetime import datetime
import ast
import json
from pathlib import Path
import re

TOPICS = Path(__file__).with_name('topics')
# BU10 keeps the full capability explanation in one discoverable topic.
MAX_TOPIC_CHARS = 12000
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_UNSAFE = re.compile(r'(?i)(bearer\s+\S+|(?:sk-|ghp_|eyJ)[A-Za-z0-9_./+-]{12,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?:[A-Z]:[\\/]|/(?:srv|home|var|etc|run)/)\S*)')


def public_text(value, limit=160):
    return _UNSAFE.sub('[redacted]', ' '.join(str(value or '').split()))[:limit]


def public_id(value):
    value = str(value or '')
    return value if _SAFE_ID.fullmatch(value) and not _UNSAFE.search(value) else '[redacted]'


async def effective_tools(registry, face_id):
    """Exactly the available effective allowlist, without changing permissions."""
    from core.tools.registry import get_all_tools
    from core.permissions import check_tool_access
    tools = await get_all_tools([t for t in registry.get_allowed_tools(face_id) if check_tool_access(face_id,t)])
    from core.execution.workflow_context import current
    workflow = current.get()
    if workflow:
        tools = [t for t in tools if workflow.permits_tool(t.name) and
                 (workflow.bound_names is None or t.name in workflow.bound_names)]
    return sorted(tools, key=lambda t: t.name)


def _model_line(row):
    return (public_id(row.get('backend')) + ': model='
            + public_id(row.get('model') or 'configured-default')
            + ', kind=' + public_id(row.get('kind'))
            + ', available=' + str(bool(row.get('available'))).lower()
            + ', enrolled=' + ', '.join(public_id(m) for m in row.get('models', [])))


def _face_model_line(face):
    line = public_id(face.id) + ': default=' + public_id(face.preferred_backend or 'automatic')
    for name in ('cc_posture', 'agy_posture', 'codex_posture', 'pi_posture'):
        model = getattr(face, name, {}).get('model')
        if model:
            line += '; ' + name + '.model=' + public_id(model)
    return line


def _broker_defaults():
    """Read only named numeric source constants, never execute the broker."""
    try:
        source = Path(__file__).resolve().parents[3] / 'portal-broker/portal-broker.py'
        lines = []
        for node in ast.parse(source.read_text(encoding='utf-8')).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in {'STREAM_SECONDS', 'QUESTION_WAIT_SECONDS'}):
                value = ast.literal_eval(node.value)
                if type(value) in (int, float):
                    lines.append('Broker shipped default ' + node.targets[0].id + '=' + str(value))
        return '\n'.join(lines)
    except (OSError, ValueError, SyntaxError):
        return 'Broker source defaults unavailable; inspect the client deadline receipt.'


async def render(topic='index', *, registry=None, face_id=None,
                 visible_faces=None, backend_rows=None):
    from core.faces.registry import get_default_registry
    from core.lane_config import configured_mode
    from core.config import config
    if not isinstance(topic, str):
        raise ValueError('guide topic must be a string')
    registry = registry if registry is not None else get_default_registry()
    visible = [f.id for f in registry.all_faces()
               if visible_faces is None or f.id in visible_faces]
    if face_id is not None and face_id not in visible:
        raise PermissionError('face outside guide scope')
    selected = face_id or (visible[0] if visible else None)
    files = {p.stem: p for p in sorted(TOPICS.glob('*.md'))}
    if topic == 'index':
        content = '\n'.join(f'{name}: {p.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")}'
                            for name, p in files.items())
    elif topic not in files:
        raise ValueError('unknown guide topic; read index')
    else:
        content = files[topic].read_text(encoding='utf-8').strip()
        if topic == 'agents':
            content = '\n'.join(line for line in content.splitlines()
                if not re.match(r'^[a-z][a-z0-9_-]*: ', line) or line.split(':', 1)[0] in visible)
            custom = [f for f in visible if not any(line.startswith(f + ': ') for line in content.splitlines())]
            content += ''.join('\n' + public_id(f) + ': ' + public_text(registry.get_face(f).blurb or registry.get_face(f).name) for f in custom)
        elif topic == 'tools':
            tools = await effective_tools(registry, selected) if selected else []
            content += '\n\n' + '\n'.join(public_id(t.name) + ': ' + public_text(t.description.split('\n\n', 1)[0], 110) for t in tools)
        elif topic == 'memory':
            content += '\n\nAutomatic Prior context character cap=' + str(config.MEMORY_CONTEXT_MAX_CHARS)
        elif topic == 'models':
            if backend_rows is None:
                from core.backends.local_router import LocalModelRouter
                if configured_mode():
                    from core.lane_config import catalog
                    backend_rows = await catalog(LocalModelRouter())
                else:
                    # Reuse the existing deployment catalog implementation; do
                    # not duplicate provider/model/availability policy here.
                    from api.server import backends_catalog, ROUTER_KEY
                    from types import SimpleNamespace
                    response = await backends_catalog(SimpleNamespace(app={ROUTER_KEY: LocalModelRouter()}))
                    backend_rows = json.loads(response.text)
            content += '\n\n' + '\n'.join(_model_line(row) for row in backend_rows)
            content += '\n' + '\n'.join(_face_model_line(registry.get_face(f)) for f in visible)
        elif topic == 'limits':
            from core.builder.service import APPROVAL_WAIT, COMMAND_DEFAULT_TIMEOUT, COMMAND_MAX_TIMEOUT
            content += '\n\n' + '\n'.join(public_id(f) + ': tool rounds=' + str(registry.get_face(f).tool_loop_max_rounds or config.TOOL_LOOP_MAX_ROUNDS) for f in visible)
            content += f'\nBuilder approval wait seconds={APPROVAL_WAIT}; command default seconds={COMMAND_DEFAULT_TIMEOUT}; maximum seconds={COMMAND_MAX_TIMEOUT}.'
            from core.tools.ask import DEFAULT_TIMEOUT_S
            content += '\nOperator question wait seconds=' + str(DEFAULT_TIMEOUT_S)
            content += '\n' + _broker_defaults()
        elif topic == 'permissions':
            from core.permissions import _ALWAYS_HUMAN_ACTIONS
            content += '\n\nAlways-human floor: ' + ', '.join(sorted(_ALWAYS_HUMAN_ACTIONS))
        elif topic in {'overview', 'changes'}:
            now = datetime.now().astimezone()
            content += '\n\nServer time: ' + now.isoformat() + '; timezone: ' + public_text(now.tzname(), 64)
            try:
                version = json.loads(Path('/etc/bob/runtime-version').read_text(encoding='utf-8'))
                if not isinstance(version, dict):
                    raise ValueError('invalid build metadata')
                content += '\nBuild: ' + public_id(version.get('build_id')) + '; revision: ' + public_id(version.get('git_rev'))
            except (OSError, ValueError, TypeError):
                content += '\nBuild metadata unavailable in this source checkout.'
    content = _UNSAFE.sub('[redacted]', content)
    if len(content) > MAX_TOPIC_CHARS:
        # Never silently truncate policy or a tool list. Split the topic file.
        raise ValueError('guide topic exceeds size budget; split this topic')
    return dict(topic=topic, face=public_id(selected) if selected else None, content=content)
