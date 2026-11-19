"""Small, versioned wire contract; no caller-selected authority."""
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
import stat
import time

MAX_REQUEST = 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_TIMEOUT = 2147483647  # finite representation bound, never selection authority
PROFILES = frozenset({'shell', 'python', 'git', 'codex', 'claude', 'mcp', 'auth-replace', 'acceptance', 'proposal', 'checked', 'builder'})
FIELDS = {'version', 'job_id', 'profile', 'workspace', 'cwd', 'args', 'input',
          'timeout', 'output_limit', 'expires'}
V2_FIELDS = (FIELDS - {'job_id', 'expires'}) | {
    'op', 'execution_id', 'logical_job_id', 'request_digest', 'lane_generation',
    'controller_session', 'profile_revision', 'not_after', 'submit_expires_at'}


def request_digest(request):
    # Ownership and dispatch deadlines are frozen at intent, included in identity.
    value = {k: v for k, v in request.items() if k != 'request_digest'}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def profile_revision(profile):
    return request_digest(profile)


def validate_v2(request, *, now=None, expired_ok=False, max_timeout=MAX_TIMEOUT):
    if not isinstance(request, dict) or set(request) != V2_FIELDS or type(request.get('version')) is not int or request['version'] != 2 or request.get('op') != 'submit':
        raise ValueError('invalid v2 submission')
    for key in ('execution_id', 'logical_job_id', 'controller_session'):
        if not isinstance(request[key], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', request[key]):
            raise ValueError('invalid execution identity')
    if type(request['lane_generation']) is not int or not 1 <= request['lane_generation'] <= 9223372036854775807:
        raise ValueError('invalid lane generation')
    for key in ('profile_revision', 'request_digest'):
        if not isinstance(request[key], str) or not re.fullmatch('[0-9a-f]{64}', request[key]):
            raise ValueError('invalid digest')
    if request_digest(request) != request['request_digest']:
        raise ValueError('request_digest_conflict')
    if len(json.dumps(request).encode()) > MAX_REQUEST:
        raise ValueError('worker request too large')
    now = time.time() if now is None else now
    for key in ('not_after', 'submit_expires_at'):
        if type(request[key]) not in (int, float) or not math.isfinite(request[key]):
            raise ValueError('invalid deadline')
    if not expired_ok and not now < request['submit_expires_at'] <= min(now + 30, request['not_after']):
        raise ValueError('expired submission')
    if not expired_ok and not now < request['not_after'] <= now + max_timeout + 30:
        raise ValueError('invalid execution deadline')
    old = {k: request[k] for k in FIELDS - {'job_id', 'expires'}}
    old.update(version=1, job_id=request['execution_id'], expires=now + 1)
    validate(old, now=now, max_timeout=max_timeout)
    if request['profile'] == 'auth-replace':
        raise ValueError('auth publication uses separate socket')
    return request


def validate_control(value):
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 2:
        raise ValueError('v2 negotiation required')
    op = value.get('op')
    fields = {'version', 'op'} if op == 'hello' else {'version', 'op', 'lane_generation', 'controller_session'}
    if op in {'inspect', 'cancel'}:
        fields |= {'execution_id'}
    if op not in {'hello', 'inspect', 'cancel', 'idle_barrier'} or set(value) != fields:
        raise ValueError('invalid control operation')
    if op != 'hello':
        if type(value['lane_generation']) is not int or not 1 <= value['lane_generation'] <= 9223372036854775807:
            raise ValueError('invalid generation')
        for key in fields - {'version', 'op', 'lane_generation'}:
            if not isinstance(value[key], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value[key]):
                raise ValueError('invalid ownership')
    return value


class WorkerUnavailable(RuntimeError):
    pass


def validate(request: dict, *, now=None, max_timeout=900) -> dict:
    if not isinstance(request, dict) or set(request) != FIELDS:
        raise ValueError('invalid worker request fields')
    if type(request['version']) is not int or request['version'] != 1:
        raise ValueError('unsupported worker protocol')
    for key in ('job_id', 'workspace'):
        if not isinstance(request[key], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', request[key]):
            raise ValueError('invalid worker identifier')
    if not isinstance(request['profile'], str) or request['profile'] not in PROFILES:
        raise ValueError('execution profile unavailable')
    cwd = request['cwd']
    if not isinstance(cwd, str) or '\\' in cwd or cwd.startswith('/') or '..' in cwd.split('/'):
        raise ValueError('invalid workspace relative cwd')
    args = request['args']
    if not isinstance(args, list) or len(args) > 256 or any(
            not isinstance(a, str) or '\0' in a or len(a.encode()) > 65536 for a in args):
        raise ValueError('invalid arguments')
    if not isinstance(request['input'], str) or '\0' in request['input']:
        raise ValueError('invalid job input')
    for key, low, high in (('timeout', 0.1, max_timeout), ('output_limit', 1, MAX_OUTPUT)):
        v = request[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError('invalid resource ceiling')
    if type(request['output_limit']) is not int:
        raise ValueError('invalid byte ceiling')
    expiry = request['expires']
    now = time.time() if now is None else now
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or not now < expiry <= now + max_timeout + 30:
        raise ValueError('expired or invalid authorization')
    if len(json.dumps(request).encode()) > MAX_REQUEST:
        raise ValueError('worker request too large')
    return request


def root_json(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise WorkerUnavailable('worker configuration must be root-owned and immutable')
    return json.loads(path.read_text())


def peer_lane(uid: int, config: dict) -> str:
    if uid != config['service_uid']:
        raise PermissionError('worker peer denied')
    return config['lane_id']


def resolve_profile(config: dict, request: dict) -> tuple[list[str], Path, bool]:
    from core.portal_resources import ResourceError
    try:
        return _resolve_profile(config, request)
    except WorkerUnavailable:
        raise
    except (ResourceError, OSError, ValueError, KeyError, RuntimeError) as exc:
        # Filesystem/resource services use different exception families. Keep
        # every request-local resolution failure inside the worker contract.
        raise WorkerUnavailable('workspace resolution refused') from exc


def _resolve_profile(config: dict, request: dict) -> tuple[list[str], Path, bool]:
    profile = config['profiles'].get(request['profile'])
    if not profile or request['profile'] not in PROFILES:
        raise WorkerUnavailable('execution profile unavailable')
    workspace = request['workspace']
    if workspace.startswith('run_'):
        identifier = workspace[4:]
        if not re.fullmatch('[0-9a-f]{32}', identifier) or not config.get('run_workspace_root'):
            raise WorkerUnavailable('run workspace unavailable')
        from core.portal_resources import safe_open
        with safe_open(Path(config['run_workspace_root']), (identifier,), directory=True) as (_, selected, info):
            if info.st_uid != config['service_uid'] or info.st_mode & 0o002:
                raise WorkerUnavailable('run workspace protection unavailable')
            root = selected.resolve(strict=True)
    else:
        root = Path(config['workspaces'][workspace]).resolve(strict=True)
    cwd = (root / request['cwd']).resolve(strict=True)
    if not cwd.is_relative_to(root) or not cwd.is_dir():
        raise ValueError('workspace escape refused')
    if request['profile'] == 'builder':
        from .builder_profile import project_root
        selected = project_root(config, request)
        if (cwd != selected or request['args']
                or bool(profile.get('network')) != (profile.get('egress') == 'lane-proxy/1')
                or profile.get('network') and not config.get('egress_port')
                or profile.get('adapter_revision') != 'builder-command/1'):
            raise WorkerUnavailable('invalid builder profile boundary')
    if request['profile'] == 'claude':
        if profile.get('argv') != ['/usr/local/bin/claude']:
            raise WorkerUnavailable('invalid fixed Claude executable')
        # Auth belongs exclusively to the root setup helper, outside the
        # durable job-input journal. A pasted code must never enter a job.
        if request['args'] not in (['--version'], ['--help']):
            raise WorkerUnavailable('Claude auth requires root setup; turns require the private CC2 turn socket')
    argv = profile['argv']
    if not argv or not PurePosixPath(argv[0]).is_absolute():
        raise ValueError('invalid installed profile')
    return [*argv, *request['args']], cwd, bool(profile.get('network', False))


def child_env(config: dict, temporary: str, *, codex=False, claude=False) -> dict[str, str]:
    # Descendants get a disposable home inside this job's private temporary rule.
    # The separately installed CODEX_HOME grant remains explicit below.
    home = temporary
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'HOME': home, 'XDG_CONFIG_HOME': home + '/.config',
           'XDG_CACHE_HOME': home + '/.cache', 'XDG_DATA_HOME': home + '/.local/share',
           'TMPDIR': temporary, 'PYTHONIOENCODING': 'utf-8',
           'PYTHONDONTWRITEBYTECODE': '1', 'GIT_CONFIG_NOSYSTEM': '1',
           'PLAYWRIGHT_BROWSERS_PATH': '/opt/bob-browsers'}
    if codex:
        env['CODEX_HOME'] = config['codex_home']
    if claude:
        from .claude import environment
        env.update(environment(config['claude_home'], temporary))
    return env


def profile_proxy_env(config, profile, proxy_url):
    """Only the installed network profiles accept exact lane-proxy credentials."""
    if profile not in {'codex', 'claude', 'mcp'}:
        if proxy_url is not None:
            raise WorkerUnavailable('proxy metadata refused for this profile')
        return {}
    installed = config.get('profiles', {}).get(profile, {})
    port = config.get('egress_port')
    if (installed.get('network') is not True or installed.get('egress') != 'lane-proxy/1'
            or type(port) is not int or not 1024 <= port <= 65535
            or not isinstance(proxy_url, str)
            or not re.fullmatch(r'http://bob:[0-9a-f]{64}@127\.0\.0\.1:' + str(port), proxy_url)):
        raise WorkerUnavailable('unavailable: profile requires a lane proxy lease')
    return {**{key: proxy_url for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy',
                'https_proxy', 'ALL_PROXY', 'all_proxy')}, 'NO_PROXY': '', 'no_proxy': ''}
