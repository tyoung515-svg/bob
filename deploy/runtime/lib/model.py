"""Root-only, lane-isolated credential setup for the desktop runtime."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import secrets
import signal
import stat
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "bobclaw-core"))
from core.worker import mode as execution_mode
from core.worker.contract import WorkerUnavailable
from core.lane_config import (read_lane, provider_setup, normalize_backend, BACKEND_FIELDS,
                              read_codex_auth, codex_signin)

from common import (ensure_codex_home, private_replace, read_env, render_env,
                    require_root, systemctl)
from lane import FAMILY, validate_name

CODEX = '/usr/local/bin/codex'
LOGIN_TIMEOUT = 15 * 60
MODEL_LOCK = '/run/bob-model-{}.lock'
SIGNIN_LOCK = '/run/bob-signin-{}.lock'
SETUP = provider_setup()
KEYS = {name: spec['key_env'] for name, spec in SETUP.items() if 'key_env' in spec}
DEFAULTS = {name: spec['backend'] for name, spec in SETUP.items()}
BACKEND_PROVIDER = {spec['backend']: name for name, spec in SETUP.items()}
BACKEND_KEYS = {backend: fields[0] for backend, fields in BACKEND_FIELDS.items()}
SUPPORTED_BACKENDS = frozenset(BACKEND_KEYS) | {
    'codex_code', 'openai_codex', 'claude_code', 'claude_alternate', 'agy_code', 'kimi_cli',
    'pi_code', 'opencode_serve', 'local',
}


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def lane_root(name: str) -> Path:
    validate_name(name)
    root = FAMILY / name / 'bob-memory'
    if not (root / 'lane.env').is_file():
        raise ValueError('unknown lane')
    return root


def save_env(path: Path, values: dict) -> None:
    if execution_mode() == "legacy":
        account = pwd.getpwnam('bob')
        private_replace(path, render_env(values), account.pw_uid, account.pw_gid)
        return
    from worker import load_identity
    ids = load_identity(path.parent.parent.name)
    # lane.env also contains gateway factors; never expose it to core even
    # transiently while regenerating the sanitized role environments.
    private_replace(path, render_env(values), 0, 0 if path.name == 'lane.env' else ids['service_gid'])
    os.chmod(path, 0o600 if path.name == 'lane.env' else 0o640)
    if path.name == 'lane.env':
        from worker import service_environments
        service_environments(path.parent, models_only=True)


def prepare(root: Path) -> Path:
    from common import writer_guard
    writer_guard(root.parent.name)
    home = ensure_codex_home(root)
    path = root / 'lane.env'
    values = read_env(path)
    if values.get('CODEX_HOME') != str(home):
        values['CODEX_HOME'] = str(home)
        save_env(path, values)
    return home


def codex_options(root: Path) -> dict:
    if execution_mode() == "legacy":
        account = pwd.getpwnam('bob')
        return {'env': {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/bob',
                        'CODEX_HOME': str(root / 'codex-home')},
                'cwd': root, 'user': account.pw_uid, 'group': account.pw_gid,
                'extra_groups': [], 'umask': 0o077}
    from worker import load_identity
    ids = load_identity(root.parent.name)
    return {'env': {'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'HOME': f'/var/lib/bob-workers/{ids["lane_id"]}/home',
                    'CODEX_HOME': str(root / 'codex-home')},
            'cwd': root / 'scratch', 'user': ids['worker_uid'], 'group': ids['worker_gid'],
            'extra_groups': [], 'umask': 0o007}


@contextmanager
def login_options(root, deadline):
    options = codex_options(root)
    if execution_mode() == 'worker':
        from worker import CONFIG, load_identity
        from core.worker.contract import root_json
        from core.worker.setup_network import setup_codex_env
        ids = load_identity(root.parent.name)
        config = root_json(CONFIG / (ids['lane_id'] + '.json'))
        with setup_codex_env(config, deadline) as env:
            yield {**options, 'env': {**options['env'], **env}}
    else:
        yield options


LOGIN_ERRORS = {
    'signin_code_rejected': 'The sign-in code was wrong or expired. Start sign-in again.',
    'signin_in_progress': 'A ChatGPT sign-in is already in progress for this lane.',
    'signin_unreachable': 'Could not reach the sign-in service through the lane proxy.',
    'signin_refused': 'The sign-in service refused the request.',
    'signin_timeout': 'The sign-in timed out.',
    'signin_cancelled': 'The sign-in was cancelled.',
    'signin_not_stored': 'The sign-in finished but no usable sign-in was stored.',
    'signin_unavailable': 'The sign-in helper or lane proxy is unavailable.',
    'signin_failed': 'The sign-in failed; try again.',
}


@contextmanager
def model_locked(name):
    # Keep the allocation -> model order used by lane/migration operators.
    from lane import LOCK_PATH
    from common import writer_guard
    with open(LOCK_PATH, 'a') as allocation, open(MODEL_LOCK.format(name), 'a') as lock:
        fcntl.flock(allocation, fcntl.LOCK_EX)
        fcntl.flock(lock, fcntl.LOCK_EX)
        writer_guard(name)
        yield


@contextmanager
def signin_locked(name):
    # Never queue a second login behind an owner who is still in the browser.
    with open(SIGNIN_LOCK.format(name), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def signin_in_progress(name):
    try:
        with open(SIGNIN_LOCK.format(name), 'r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def credential_owner(root):
    if execution_mode() == 'legacy':
        account = pwd.getpwnam('bob')
        return account.pw_uid, account.pw_gid
    from worker import load_identity
    ids = load_identity(root.parent.name)
    return ids['worker_uid'], ids['worker_gid']


def cleanup_signin_homes(root):
    """Only under signin_locked: stale homes are discarded, never recovered."""
    from core.worker.auth_file import directory
    uid, _ = credential_owner(root)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('safe sign-in cleanup unavailable')
    with directory(root) as parent:
        for name in os.listdir(parent):
            if not re.fullmatch(r'\.(?:codex|claude)-signin-[a-z0-9_]{8,64}', name):
                continue
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
                raise ValueError('unsafe stale sign-in home')
            # rmtree's fd-based walk never follows nested links. Refuse a
            # swapped top-level entry rather than traversing it as root.
            shutil.rmtree(name, dir_fd=parent)


@contextmanager
def signin_home(root):
    # The helper must not replace a key saved to the live home during its wait.
    # Keep the temporary home inside this lane and use the same worker identity.
    with tempfile.TemporaryDirectory(prefix='.codex-signin-', dir=root) as temporary:
        home = Path(temporary)
        uid, gid = credential_owner(root)
        os.chown(home, uid, gid)
        private_replace(home / 'config.toml', 'cli_auth_credentials_store = "file"\n', uid, gid)
        yield home


def read_signin_auth(root, home):
    from core.worker.auth_file import read
    try:
        return read(home / 'auth.json', credential_owner(root)[0])
    except FileNotFoundError:
        return {}


def store_signin_auth(root, auth):
    # Only the new sign-in fields come from the helper. Preserve a current key,
    # including one saved after the helper started. Never replay old lane envs.
    current = read_codex_auth(read_lane(root))
    value = dict(auth)
    if current.get('OPENAI_API_KEY'):
        value['OPENAI_API_KEY'] = current['OPENAI_API_KEY']
    uid, gid = credential_owner(root)
    private_replace(root / 'codex-home/auth.json', json.dumps(value) + '\n', uid, gid)
    if execution_mode() == 'worker':
        share_auth(root)


def signin_root(name):
    root = lane_root(name)
    if (root / '.disabled').exists():
        raise ValueError('lane was removed during sign-in')
    return root


def login_error(code):
    emit({'event': 'error', 'code': code, 'detail': LOGIN_ERRORS[code]})


def login_failure(status, output):
    text = output.lower()
    if status in {-2, -15, 130, 143} or any(p in text for p in ('cancelled', 'canceled', 'access_denied')):
        return 'signin_cancelled'
    if any(p in text for p in ('timed out', 'timeout', 'expired_token', 'device code expired')):
        return 'signin_timeout'
    if any(p in text for p in ('403', '401', 'forbidden', 'unauthorized', 'device_auth_not_enabled')):
        return 'signin_refused'
    if any(p in text for p in ('error sending request', 'could not resolve', 'connection refused', 'connection reset', 'dns error', 'proxy authentication')):
        return 'signin_unreachable'
    return 'signin_failed' if status else 'signin_not_stored'


@contextmanager
def login_cancellation():
    def cancel(signum, frame):
        raise KeyboardInterrupt
    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {number: signal.signal(number, cancel) for number in signals}
    try:
        yield None
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def share_auth(root):
    from worker import load_identity
    from core.worker.auth_file import directory, checked
    ids = load_identity(root.parent.name)
    with directory(root / 'codex-home') as dfd:
        fd = os.open('auth.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        try:
            checked(fd, ids['worker_uid'])
            os.fchmod(fd, 0o660)
        finally:
            os.close(fd)


def openai_credentials(root: Path) -> tuple[bool, bool]:
    """Inspect only this lane's Codex auth file; never inherit root/host login."""
    try:
        auth = read_codex_auth(read_lane(root))
        return codex_signin(auth), bool(auth.get('OPENAI_API_KEY'))
    except (OSError, ValueError, AttributeError):
        return False, False


def status(name: str) -> dict:
    root = lane_root(name)
    lane = read_lane(root)
    providers = lane.values
    signin, key = openai_credentials(root)
    credentials = {'openai_signin': signin, 'openai_key': key,
                   **{p + '_key': bool(providers.get(k)) for p, k in KEYS.items()}}
    default = lane.default_backend
    ready = lane.default_error() is None
    result = {'lane': name, 'providers': credentials, 'default_backend': default, 'ready': ready,
              'chatgpt': chatgpt_status(lane), 'claude': claude_status(root),
              'signin_in_progress': signin_in_progress(name)}
    if default and not ready:
        result['error'] = lane.default_error()
    return result


def chatgpt_status(lane) -> dict:
    auth = read_codex_auth(lane)
    refreshed = None
    # Emit a parsed timestamp, never arbitrary auth-file text or account IDs.
    try:
        value = auth.get('last_refresh')
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                refreshed = stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        pass
    row = lane.backends.get('openai_codex')
    selected = row.model if row else None
    return {'signed_in': codex_signin(auth), 'selected_model': selected,
            'last_refresh': refreshed, 'model_selection_required': codex_signin(auth) and not selected}


def visible_openai_models(root: Path, *, lane=None) -> list[str]:
    from core.backends.openai_codex import OpenAICodexClient
    return asyncio.run(OpenAICodexClient(lane=lane or read_lane(root)).visible_models())


def openai_models(name: str, *, lane=None) -> dict:
    root = lane_root(name)
    state = chatgpt_status(lane or read_lane(root))
    result = {'ok': False, 'lane': name, 'backend': 'openai_codex',
              **state, 'account_default': None, 'models': []}
    if not state['signed_in']:
        result['error'] = {'code': 'chatgpt_signin_required', 'detail': 'Sign in with ChatGPT first.'}
        return result
    from core.backends.openai_codex_auth import CodexProviderError
    try:
        models = visible_openai_models(root, lane=lane) if lane is not None else visible_openai_models(root)
    except (CodexProviderError, OSError, ValueError, ImportError):
        result['error'] = {'code': 'chatgpt_models_unavailable',
                           'detail': 'Cannot read ChatGPT models. Retry, or sign in again if the session expired.'}
        return result
    result.update(ok=True, account_default=models[0], models=[
        {'slug': slug, 'account_default': i == 0, 'selected': slug == state['selected_model']}
        for i, slug in enumerate(models)])
    return result


def save_openai_models(root: Path, listing: dict, selected: str) -> None:
    from core.backends.openai_codex_models import enrolled_models
    values = read_env(root / 'lane.env')
    values['OPENAI_CODEX_MODEL'] = selected
    values['OPENAI_CODEX_MODELS'] = json.dumps(enrolled_models(
        [row['slug'] for row in listing['models']], selected), separators=(',', ':'))
    save_env(root / 'lane.env', values)


def set_openai_model(name: str, slug: str) -> dict:
    with model_locked(name):
        initial = read_codex_auth(read_lane(lane_root(name)))
    listing = openai_models(name)
    if not listing['ok']:
        return listing
    if slug not in {row['slug'] for row in listing['models']}:
        return {**listing, 'ok': False, 'error': {'code': 'chatgpt_model_not_visible',
                'detail': 'Choose a model from the signed-in account model list.'}}
    with model_locked(name):
        root = lane_root(name)
        if read_codex_auth(read_lane(root)) != initial:
            return {**listing, 'ok': False, 'error': {'code': 'chatgpt_models_unavailable',
                    'detail': 'Cannot read ChatGPT models. Retry, or sign in again if the session expired.'}}
        save_openai_models(root, listing, slug)
        result = {**listing, **chatgpt_status(read_lane(root)), 'models': [
            {**row, 'selected': row['slug'] == slug} for row in listing['models']]}
    systemctl('restart', f'bob-core@{name}.service')
    return result


def complete_openai_setup(root: Path, *, listing=None) -> dict:
    save_preset(root, 'openai')
    if listing is None:
        listing = openai_models(root.parent.name)
    selected = chatgpt_status(read_lane(root))['selected_model']
    if listing['ok']:
        save_openai_models(root, listing, selected or listing['account_default'])
    result = chatgpt_status(read_lane(root))
    if not listing['ok']:
        result['model_notice'] = 'ChatGPT sign-in saved. Read the model list and choose a GPT model.'
    return result


def set_default(root: Path, backend: str, *, only_if_missing: bool = False) -> str:
    backend = normalize_backend(backend)
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError('unsupported lane backend')
    lane = read_lane(root)
    lane.require(backend)
    path = root / 'lane.env'
    values = read_env(path)
    if only_if_missing and lane.default_backend:
        if lane.default_error():
            raise ValueError(lane.default_error())
        return lane.default_backend
    if not only_if_missing or not values.get('BOB_LANE_DEFAULT_BACKEND'):
        values['BOB_LANE_DEFAULT_BACKEND'] = backend
        save_env(path, values)
    return values['BOB_LANE_DEFAULT_BACKEND']


def save_preset(root: Path, provider: str) -> None:
    """Persist setup data; existing explicit model overrides and defaults win."""
    spec = SETUP[provider]
    path = root / 'lane.env'
    values = read_env(path)
    backends = [normalize_backend(b.strip()) for b in values.get('BOB_LANE_BACKENDS', '').split(',') if b.strip()]
    if spec['backend'] not in backends:
        backends.append(spec['backend'])
    values['BOB_LANE_BACKENDS'] = ','.join(backends)
    if spec['model'] is not None:
        values.setdefault(spec['model_env'], spec['model'])
    save_env(path, values)


def read_key(stream) -> str:
    value = stream.read()
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    if value.endswith('\r\n'):
        value = value[:-2]
    elif value.endswith('\n'):
        value = value[:-1]
    if not value or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in value):
        raise ValueError('key must be nonempty and contain no control characters')
    return value


def storage_key(provider, key):
    """Validate vault bytes; the settings journal then enrolls the preset."""
    from core.settings_contract import validate
    validate(dict(version=1, op='secrets.set', provider=provider, key=key, request_key='validation'))
    return key


def enroll_provider(name: str, provider: str) -> bool:
    """Write enrollment and report whether the running lane needs a reload."""
    from lane import LOCK_PATH
    from common import writer_guard
    # Match model/migration lock order. Settings durably records restart debt
    # before entry: a crash after writing envs must not lose the owed restart.
    with open(LOCK_PATH, 'a') as allocation, open(f'/run/bob-model-{name}.lock', 'a') as lock:
        fcntl.flock(allocation, fcntl.LOCK_EX)
        fcntl.flock(lock, fcntl.LOCK_EX)
        writer_guard(name)
        root = lane_root(name)
        # Worker lanes load core.env. Legacy lanes load lane.env directly.
        environment = root / ('lane.env' if execution_mode() == 'legacy' else 'core.env')
        before = environment.read_bytes() if environment.exists() else None
        save_preset(root, provider)
        set_default(root, DEFAULTS[provider], only_if_missing=True)
        return before != environment.read_bytes()


def restart_provider(name: str) -> None:
    # Requires= on the gateway unit means this core restart also restarts the
    # gateway. The control socket must finish its reply before calling here.
    systemctl('restart', f'bob-core@{name}.service')


def set_key(name: str, provider: str, key: str) -> dict:
    root = lane_root(name)
    values = read_env(root / 'lane.env')
    lane_id = values.get('BOB_SETTINGS_ID') or values.get('BOB_WORKER_ID')
    if provider in KEYS and lane_id:
        # Root-authorized native entry uses the same vault/journal as the app.
        import settings
        cfg = settings.config(lane_id)
        if cfg['lane'] != name:
            raise ValueError('invalid lane')
        settings.handle(cfg, cfg['gateway_uid'], dict(version=1, op='secrets.set',
            provider=provider, key=key, request_key=secrets.token_hex(16)))
        return {'ok': True, 'default_backend': read_lane(root).default_backend}
    prepare(root)
    if provider == 'openai':
        try:
            result = subprocess.run([CODEX, 'login', '--with-api-key'], input=key,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=60, **codex_options(root))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Codex key login failed; output withheld') from None
        if execution_mode() == "worker" and not result.returncode:
            share_auth(root)
        if result.returncode or not openai_credentials(root)[1]:
            raise ValueError('Codex key login failed; output withheld')
    else:
        if provider not in KEYS:
            raise ValueError('unsupported provider')
        path = root / 'providers.env'
        values = read_env(path) if path.exists() else {}
        values[KEYS[provider]] = key
        save_env(path, values)
    save_preset(root, provider)
    default = set_default(root, DEFAULTS[provider], only_if_missing=True)
    systemctl('restart', f'bob-core@{name}.service')
    return {'ok': True, 'default_backend': default}


def device_code(output: str) -> dict | None:
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    urls = re.findall(r'https://[A-Za-z0-9./_?=&%+-]+', text)
    code = re.search(r'\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b', text)
    if urls and code:
        url = next((url for url in urls if '/device' in url), urls[-1])
        return {'event': 'device_code', 'url': url, 'code': code.group()}
    return None


def device_login(root: Path, home: Path) -> bool:
    # Merge stderr into stdout: Codex versions differ in which stream carries
    # the URL/code. Read bytes as they arrive, without waiting for a newline.
    output = ''
    sent = False
    deadline = time.monotonic() + LOGIN_TIMEOUT
    try:
        with login_cancellation(), login_options(root, deadline) as options, subprocess.Popen([CODEX, 'login', '--device-auth'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              **{**options, 'env': {**options['env'], 'CODEX_HOME': str(home)}}) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired('codex login', LOGIN_TIMEOUT)
                        for registered, _ in selector.select(min(remaining, 1)):
                            chunk = os.read(registered.fd, 4096)
                            if not chunk:
                                selector.unregister(registered.fileobj)
                                continue
                            output = (output + chunk.decode('utf-8', 'replace'))[-16384:]
                            event = device_code(output)
                            if event and not sent:
                                emit(event)
                                sent = True
                result = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if result or not codex_signin(read_signin_auth(root, home)):
            login_error(login_failure(result, output))
            return False
    except subprocess.TimeoutExpired:
        login_error('signin_timeout')
        return False
    except KeyboardInterrupt:
        login_error('signin_cancelled')
        return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, WorkerUnavailable):
        login_error('signin_unavailable')
        return False
    return True


def openai_login(name: str) -> bool:
    with signin_locked(name) as available:
        if not available:
            login_error('signin_in_progress')
            return False
        try:
            with model_locked(name):
                root = signin_root(name)
                prepare(root)
                owner = credential_owner(root)
            cleanup_signin_homes(root)
            with login_cancellation(), signin_home(root) as home:
                if not device_login(root, home):
                    return False
                auth = read_signin_auth(root, home)
                # The catalog is also a network wait. Inspect the temporary
                # credentials without publishing them or holding either lock.
                lane = read_lane(root)
                staged = read_lane(root, values={**lane.values, 'CODEX_HOME': str(home)})
                listing = openai_models(name, lane=staged)
                with model_locked(name):
                    root = signin_root(name)
                    prepare(root)
                    if credential_owner(root) != owner:
                        raise ValueError('lane identity changed during sign-in')
                    store_signin_auth(root, auth)
                    chatgpt = complete_openai_setup(root, listing=listing)
                    set_default(root, 'openai_codex', only_if_missing=True)
            # Restart waits for systemd, so keep it outside both shared locks.
            systemctl('restart', f'bob-core@{name}.service')
        except KeyboardInterrupt:
            login_error('signin_cancelled')
            return False
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, WorkerUnavailable):
            login_error('signin_unavailable')
            return False
        emit({'event': 'done', 'ok': True, 'chatgpt': chatgpt})
        return True


# Claude subscription setup deliberately never imports the legacy turn backend.
from core.worker.claude import (EXECUTABLE as CLAUDE, CREDENTIAL as CLAUDE_CREDENTIAL,
    MODELS as CLAUDE_MODELS, environment as claude_environment, status_json as claude_status_json,
    signin_url as claude_signin_url, code_line as claude_code_line,
    code_rejected as claude_code_rejected, MAX_CODE as CLAUDE_MAX_CODE)


def claude_error(code):
    detail = ('A sign-in is already in progress for this lane.' if code == 'signin_in_progress'
              else LOGIN_ERRORS[code])
    emit(dict(event='error', code=code, detail=detail))


def claude_config(root):
    # Never accept a CLI path/config override from a lane or inherited environment.
    return root / 'cli/claude'


def prepare_claude(root):
    from core.worker.auth_file import directory
    if execution_mode() != 'worker':
        raise WorkerUnavailable('Claude subscription setup requires a worker lane')
    from worker import load_identity, provision_claude
    ids = load_identity(root.parent.name)
    config = provision_claude(ids, root)
    with directory(config / 'home', create=True) as fd:
        os.fchown(fd, ids['worker_uid'], ids['worker_gid'])
        os.fchmod(fd, 0o700)
    return config


def claude_options(root, config, installed):
    from core.worker.auth_file import directory
    uid, gid = credential_owner(root)
    home = config / 'home'
    with directory(home, create=True) as fd:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o700)
    def confine():
        from core.worker.daemon import policy_ready
        policy_ready(installed)
        from core.worker.restrict import install
        from core.worker.landlock import install as landlock
        install(network=True)
        landlock(config, home)
    return dict(env=claude_environment(config, home), cwd=home,
                user=uid, group=gid, extra_groups=[], umask=0o077,
                preexec_fn=confine, start_new_session=True)


@contextmanager
def claude_setup_options(root, config, deadline):
    from worker import CONFIG, load_identity
    from core.worker.contract import root_json
    from core.worker.setup_network import setup_cli_env
    ids = load_identity(root.parent.name)
    installed = root_json(CONFIG / (ids['lane_id'] + '.json'))
    # The UID firewall also applies to these root-launched worker children.
    # Verify its guardian at every setup launch; there is no direct fallback.
    with setup_cli_env(installed, deadline, 'claude') as proxy:
        options = claude_options(root, config, installed)
        yield {**options, 'env': {**options['env'], **proxy}}


def stop_claude(process):
    # The auth helper starts a session; close descendants before deleting its home.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


class ClaudeAuthNotStored(ValueError):
    pass


def checked_claude_auth(root, config):
    from core.worker.auth_file import read
    value = read(config / CLAUDE_CREDENTIAL, credential_owner(root)[0])
    tokens = value.get('claudeAiOauth')
    if not isinstance(tokens, dict) or not any(
            isinstance(tokens.get(key), str) and tokens[key] for key in ('accessToken', 'refreshToken')):
        raise ValueError('subscription credentials not stored')
    return value


def claude_auth_status(root, config, *, deadline=None):
    # Inspect a present credential before the CLI can open it. Missing is a
    # valid logged-out state; a link/wrong owner/invalid inode is unavailable.
    try:
        checked_claude_auth(root, config)
    except FileNotFoundError:
        pass
    deadline = deadline or time.monotonic() + 15
    with claude_setup_options(root, config, deadline) as options:
        # Private temporary files bound memory use; raw output is never forwarded.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen([CLAUDE, 'auth', 'status'], stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=errors, **options) as process:
                try:
                    result = process.wait(timeout=max(.01, deadline - time.monotonic()))
                finally:
                    stop_claude(process)
            output.seek(0)
            state = claude_status_json(output.read(65537))
    if result not in (0, 1):
        raise ValueError('Claude status unavailable')
    if state['signed_in']:
        if result != 0:
            raise ValueError('inconsistent Claude status')
        try:
            checked_claude_auth(root, config)
        except FileNotFoundError:
            raise ClaudeAuthNotStored('subscription sign-in not stored') from None
    return state


def claude_status(root):
    values = read_env(root / 'lane.env')
    selected = values.get('CLAUDE_CODE_MODEL')
    selected = selected if selected in CLAUDE_MODELS else None
    try:
        if not claude_config(root).exists():
            return dict(signed_in=False, selected_model=selected, last_checked=None,
                        model_selection_required=False)
        state = claude_auth_status(root, claude_config(root))
        checked = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (OSError, ValueError, KeyError, TypeError, ImportError, WorkerUnavailable,
            subprocess.SubprocessError):
        # Unknown is never represented as an authenticated account.
        return dict(signed_in=False, selected_model=selected, last_checked=None,
                    model_selection_required=False)
    from core.backends.claude_state import load as claude_observations
    try:
        observation = claude_observations(root, values.get('CLAUDE_AUTH_GENERATION', ''))
    except (OSError, ValueError):
        observation = {'status': 'unknown'}
    unknown = observation.get('status') == 'unknown'
    return dict(signed_in=state['signed_in'] and not unknown, selected_model=selected, last_checked=checked,
                status='unknown' if unknown else 'signed_in' if state['signed_in'] else 'signed_out',
                provider_reported_model=observation.get('models', {}).get(selected),
                model_selection_required=state['signed_in'] and not selected)


def save_claude_state(root, signed, selected=None):
    values = read_env(root / 'lane.env')
    values['CLAUDE_SIGNED_IN'] = 'true' if signed else 'false'
    if selected is None:
        # A fresh verified sign-in/logout invalidates an old runtime uncertainty
        # marker. Model selection alone must not clear an authentication failure.
        values['CLAUDE_AUTH_GENERATION'] = secrets.token_hex(16)
    values['CLAUDE_LAST_CHECKED'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    if selected is not None:
        values['CLAUDE_CODE_MODEL'] = selected
    elif not signed:
        values.pop('CLAUDE_CODE_MODEL', None)
    values['CC_CONFIG_DIR'] = str(claude_config(root))
    values['CC_CLI_PATH'] = CLAUDE
    enrolled = [b for b in values.get('BOB_LANE_BACKENDS', '').split(',') if b]
    if signed and 'claude_code' not in enrolled:
        enrolled.append('claude_code')
    if not signed:
        enrolled = [b for b in enrolled if b != 'claude_code']
    values['BOB_LANE_BACKENDS'] = ','.join(enrolled)
    save_env(root / 'lane.env', values)


def claude_models(name):
    state = claude_status(lane_root(name))
    return dict(ok=True, lane=name, backend='claude_code', **state,
                account_default='default', list_source='declared_cli_options', models=[
        dict(slug=slug, label='Account default' if slug == 'default' else slug,
             account_default=slug == 'default', selected=state['selected_model'] == slug)
        for slug in CLAUDE_MODELS])


def set_claude_model(name, slug):
    if slug not in CLAUDE_MODELS:
        return dict(ok=False, error=dict(code='claude_model_not_declared',
                    detail='Choose a model from the declared Claude model list.'))
    with signin_locked(name) as available:
        if not available:
            return dict(ok=False, error=dict(code='signin_in_progress',
                        detail='A sign-in is already in progress for this lane.'))
        root = lane_root(name)
        state = claude_status(root)  # no allocation/model lock during CLI wait
        if not state['signed_in']:
            return dict(ok=False, error=dict(code='claude_signin_required',
                        detail='Sign in with a Claude subscription first.'))
        with model_locked(name):
            root = signin_root(name)
            save_claude_state(root, True, slug)
        systemctl('restart', f'bob-core@{name}.service')
    return dict(ok=True, lane=name, backend='claude_code', **{**state, 'selected_model': slug,
                'model_selection_required': False})


def claude_login_process(root, config, deadline):
    output, lines = '', ''
    sent, submitted, rejected = False, False, False
    code = bytearray()
    try:
        with claude_setup_options(root, config, deadline) as options:
            with subprocess.Popen([CLAUDE, 'auth', 'login', '--claudeai'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ, 'cli')
                        while selector.get_map():
                            if time.monotonic() >= deadline:
                                raise subprocess.TimeoutExpired('claude login', LOGIN_TIMEOUT)
                            for registered, _ in selector.select(min(1, max(.01, deadline-time.monotonic()))):
                                if registered.data == 'code':
                                    # Read one byte at a time: never consume a second input line.
                                    char = os.read(registered.fd, 1)
                                    if not char:
                                        raise KeyboardInterrupt
                                    code.extend(char)
                                    if len(code) > CLAUDE_MAX_CODE + 2:
                                        raise ValueError('invalid sign-in code')
                                    if char == b'\n':
                                        process.stdin.write(claude_code_line(bytes(code)))
                                        process.stdin.flush()
                                        process.stdin.close()
                                        code.clear()
                                        submitted = True
                                        selector.unregister(registered.fileobj)
                                    continue
                                chunk = os.read(registered.fd, 4096)
                                if not chunk:
                                    selector.unregister(registered.fileobj)
                                    # An early helper exit must not leave us waiting for owner input.
                                    for key in list(selector.get_map().values()):
                                        selector.unregister(key.fileobj)
                                    break
                                text = chunk.decode('utf-8', 'replace')
                                output = (output + text)[-16384:]
                                lines += text
                                if len(lines) > 32768:
                                    raise ValueError('invalid helper output')
                                while '\n' in lines:
                                    line, lines = lines.split('\n', 1)
                                    if not sent:
                                        url = claude_signin_url(line)
                                        if url:
                                            emit(dict(event='signin_link', url=url))
                                            sent = True
                                            # Only the dedicated native pipe receives the sensitive code.
                                            selector.register(sys.stdin.buffer, selectors.EVENT_READ, 'code')
                                    # Never emit CLI prose, credentials or a pasted code.
                                if submitted and claude_code_rejected(output):
                                    rejected = True
                                    for key in list(selector.get_map().values()):
                                        selector.unregister(key.fileobj)
                                    break
                        if rejected:
                            stop_claude(process)
                            result = process.returncode
                        else:
                            result = process.wait(timeout=max(.01, deadline-time.monotonic()))
                finally:
                    code.clear()
                    stop_claude(process)
        # Prose and exit code cannot establish a subscription login.
        state = claude_auth_status(root, config, deadline=deadline)
        if state['signed_in']:
            return True
        claude_error('signin_code_rejected' if submitted and claude_code_rejected(output)
                    else login_failure(result, output))
    except (subprocess.TimeoutExpired, TimeoutError):
        claude_error('signin_timeout')
    except (KeyboardInterrupt, BrokenPipeError):
        claude_error('signin_cancelled')
    except ClaudeAuthNotStored:
        claude_error('signin_not_stored')
    except ValueError:
        claude_error('signin_code_rejected' if sent and not submitted else 'signin_unavailable')
    except (OSError, KeyError, TypeError, AttributeError, ImportError,
            WorkerUnavailable, subprocess.SubprocessError):
        claude_error('signin_unavailable')
    return False


@contextmanager
def claude_signin_home(root):
    with tempfile.TemporaryDirectory(prefix='.claude-signin-', dir=root) as temporary:
        config = Path(temporary)
        os.chown(config, *credential_owner(root))
        yield config


def private_claude_replace(config, name, value, uid, gid):
    from core.worker.auth_file import directory, checked
    data = (json.dumps(value) + '\n').encode()
    if len(data) > 1024 * 1024:
        raise ValueError('oversized credential')
    with directory(config) as parent:
        temporary = '.signin-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchown(stream.fileno(), uid, gid)
                checked(stream.fileno(), uid)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def publish_claude_auth(root, config):
    from core.worker.auth_file import read
    uid, gid = credential_owner(root)
    value = checked_claude_auth(root, config)
    # Copy only two checked JSON inodes; never publish logs, settings or a home.
    optional = None
    try:
        optional = read(config / '.claude.json', uid)
    except FileNotFoundError:
        pass
    live = claude_config(root)
    if optional is not None:
        private_claude_replace(live, '.claude.json', optional, uid, gid)
    private_claude_replace(live, CLAUDE_CREDENTIAL, value, uid, gid)


def claude_login(name):
    with signin_locked(name) as available:
        if not available:
            claude_error('signin_in_progress')
            return False
        try:
            # Cover preparation, waits, publication and temporary-home cleanup.
            with login_cancellation():
                with model_locked(name):
                    root = signin_root(name)
                    prepare_claude(root)
                    owner = credential_owner(root)
                cleanup_signin_homes(root)
                deadline = time.monotonic() + LOGIN_TIMEOUT
                with claude_signin_home(root) as temporary:
                    if not claude_login_process(root, temporary, deadline):
                        return False
                    with model_locked(name):
                        root = signin_root(name)
                        if credential_owner(root) != owner:
                            raise ValueError('lane identity changed during sign-in')
                        publish_claude_auth(root, temporary)
                    # Verify the live, copied login before enrollment, without shared locks.
                    state = claude_auth_status(root, claude_config(root), deadline=deadline)
                    if not state['signed_in']:
                        claude_error('signin_not_stored')
                        return False
                    with model_locked(name):
                        save_claude_state(signin_root(name), True)
                systemctl('restart', f'bob-core@{name}.service')
                emit(dict(event='done', ok=True))
                return True
        except (KeyboardInterrupt, BrokenPipeError):
            claude_error('signin_cancelled')
        except (subprocess.TimeoutExpired, TimeoutError):
            claude_error('signin_timeout')
        except ClaudeAuthNotStored:
            claude_error('signin_not_stored')
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError,
                WorkerUnavailable, subprocess.SubprocessError):
            claude_error('signin_unavailable')
    return False


def claude_logout(name):
    with signin_locked(name) as available:
        if not available:
            return dict(ok=False, error=dict(code='signin_in_progress',
                        detail='A sign-in is already in progress for this lane.'))
        try:
            root = signin_root(name)
            deadline = time.monotonic() + 30
            with login_cancellation(), claude_setup_options(root, claude_config(root), deadline) as options:
                with subprocess.Popen([CLAUDE, 'auth', 'logout'], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      **options) as process:
                    try:
                        result = process.wait(timeout=max(.01, deadline-time.monotonic()))
                    finally:
                        stop_claude(process)
            state = claude_auth_status(root, claude_config(root), deadline=deadline)
            if result or not state.get('logged_out'):
                raise ValueError('logout not verified')
            with model_locked(name):
                save_claude_state(signin_root(name), False)
            systemctl('restart', f'bob-core@{name}.service')
            return dict(ok=True, lane=name, signed_in=False, selected_model=None)
        except (OSError, ValueError, KeyError, TypeError, ImportError, WorkerUnavailable,
                subprocess.SubprocessError, KeyboardInterrupt):
            return dict(ok=False, error=dict(code='signin_failed',
                        detail='Could not verify Claude sign-out. Try again.'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'openai-login', 'openai-models', 'openai-set-model',
                    'claude-login', 'claude-models', 'claude-set-model', 'claude-logout',
                    'set-key', 'set-default'):
        sub = commands.add_parser(command)
        sub.add_argument('lane', type=validate_name)
        if command in {'openai-set-model', 'claude-set-model'}:
            sub.add_argument('model')
        if command == 'set-key':
            sub.add_argument('provider', choices=DEFAULTS)
        if command == 'set-default':
            sub.add_argument('backend', choices=sorted(SUPPORTED_BACKENDS))
    args = parser.parse_args()
    require_root()
    if args.command == 'claude-login':
        return 0 if claude_login(args.lane) else 1
    if args.command in {'claude-models', 'claude-set-model', 'claude-logout'}:
        result = (claude_models(args.lane) if args.command == 'claude-models' else
                  set_claude_model(args.lane, args.model) if args.command == 'claude-set-model'
                  else claude_logout(args.lane))
        emit(result)
        return 0 if result['ok'] else 1
    if args.command == 'openai-login':
        return 0 if openai_login(args.lane) else 1
    if args.command in {'openai-models', 'openai-set-model'}:
        result = (openai_models(args.lane) if args.command == 'openai-models'
                  else set_openai_model(args.lane, args.model))
        emit(result)
        return 0 if result['ok'] else 1
    if args.command == 'status':
        emit(status(args.lane))
        return 0
    # Read bytes before shared locks: an owner or pipe writer can wait.
    # Universal-newline conversion must not turn a forbidden lone CR into LF.
    key = read_key(sys.stdin.buffer) if args.command == 'set-key' else None
    # The settings enrollment step takes the model locks itself. Do not hold
    # them while entering its journal (the helper uses journal -> model order).
    if (args.command == 'set-key' and args.provider in KEYS
            and any(read_env(lane_root(args.lane) / 'lane.env').get(k)
                    for k in ('BOB_SETTINGS_ID', 'BOB_WORKER_ID'))):
        emit(set_key(args.lane, args.provider, key))
        return 0
    with model_locked(args.lane):
        if args.command == 'set-key':
            emit(set_key(args.lane, args.provider, key))
        else:
            root = lane_root(args.lane)
            prepare(root)
            default = set_default(root, args.backend)
            systemctl('restart', f'bob-core@{args.lane}.service')
            emit({'ok': True, 'default_backend': default})
    return 0
