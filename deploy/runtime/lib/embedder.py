"""Root-mediated setup of Bob's shared packaged embedder; no shell or caller URLs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, build_opener

CONFIG = Path('/etc/bob')
DATA = Path('/var/lib/bob')
STATE = Path('/var/lib/bob-embedder')
DROPIN = Path('/etc/systemd/system/bob-embedder.service.d/selection.conf')
LOCK = STATE / 'lock'
MODELS = {
    '4b': dict(alias='qwen3-embedding-4b', dimension=2560,
               filename='Qwen3-Embedding-4B-Q4_K_M.gguf', size_gb=2.4,
               url='https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/main/Qwen3-Embedding-4B-Q4_K_M.gguf',
               sha256='2b0cf8f17b4c723c27303015383c27ec4bf2d8314bb677d05e920dd70bb0f16b'),
    # Official Qwen pointer SHA256, inspected 2026-11-05 (not the Xet hash).
    '0.6b': dict(alias='qwen3-embedding-0.6b', dimension=1024,
                 filename='Qwen3-Embedding-0.6B-Q8_0.gguf', size_gb=0.639,
                 url='https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf',
                 sha256='06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439'),
}


class SetupError(ValueError):
    pass


class Cancelled(BaseException):
    """Install job received SIGTERM (systemctl stop). BaseException so no handler swallows it."""


def checked_pin(value):
    url = urlsplit(value['url'])
    if (url.scheme != 'https' or url.netloc != 'huggingface.co'
            or not re.fullmatch(r'/Qwen/[A-Za-z0-9._-]+/resolve/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.gguf', url.path)
            or any(ord(char) < 33 for char in value['url']) or url.query or url.fragment
            or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
            or len(set(value['sha256'])) == 1):
        raise SetupError('Unverified embedder pin: official Qwen HTTPS URL and real SHA256 required.')
    return value


def read_public(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536
            or (os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o022))):
        raise SetupError('Unsafe embedder configuration.')
    return path.read_text(encoding='utf-8')


def catalog():
    path = CONFIG / 'embedder-models.json'
    values = json.loads(read_public(path)) if path.exists() else MODELS
    if set(values) != set(MODELS):
        raise SetupError('Invalid embedder catalog.')
    for key, value in values.items():
        checked_pin(value)
        if any(value[field] != MODELS[key][field] for field in ('alias', 'dimension', 'filename')):
            raise SetupError('Invalid embedder model identity.')
    return values


def selection():
    path = CONFIG / 'embedder.env'
    values = {}
    if path.exists():
        for line in read_public(path).splitlines():
            if not line or line.startswith('#'):
                continue
            key, sep, raw = line.partition('=')
            tokens = shlex.split(raw)
            if not sep or len(tokens) != 1:
                raise SetupError('Invalid embedder selection.')
            values[key] = tokens[0]
    key = values.get('EMBEDDER_MODEL', '4b')  # original URL/SHA-only configuration
    if key not in MODELS:
        raise SetupError('Unknown embedder selection.')
    model = catalog()[key]
    expected = dict(EMBEDDER_URL=model['url'], EMBEDDER_SHA256=model['sha256'],
                    EMBEDDER_ALIAS=model['alias'], EMBEDDER_DIM=str(model['dimension']),
                    EMBEDDER_PATH=str(DATA / 'models' / model['filename']))
    if any(name in values and values[name] != value for name, value in expected.items()):
        raise SetupError('Inconsistent embedder selection; reselect the pinned model.')
    origin = values.get('EMBEDDER_SELECTION_ORIGIN')
    if origin not in {None, 'build-default', 'operator'}:
        raise SetupError('Invalid embedder selection origin.')
    return dict(model=key, **model, origin=origin, path=str(DATA / 'models' / model['filename']))


def migrate_selection():
    """Complete a legacy env without replacing operator URL/SHA pins or extra fields.

    The upgrade coordinator snapshots this env and drop-in before invoking hooks.
    Replays are safe, including interruption between the two atomic publications.
    """
    path = CONFIG / 'embedder.env'
    values = {}
    original = read_public(path) if path.exists() else ''
    for line in original.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, sep, raw = line.partition('=')
        tokens = shlex.split(raw)
        if not sep or len(tokens) > 1:
            raise SetupError('Invalid embedder selection.')
        values[name] = tokens[0] if tokens else ''
    models = catalog()
    key = values.get('EMBEDDER_MODEL') or next(
        (key for key, model in models.items() if model['url'] == values.get('EMBEDDER_URL')), '4b')
    if key not in models:
        raise SetupError('Unknown embedder selection.')
    model = models[key]
    required = dict(EMBEDDER_MODEL=key, EMBEDDER_PATH=str(DATA / 'models' / model['filename']),
                    EMBEDDER_ALIAS=model['alias'], EMBEDDER_DIM=str(model['dimension']))
    for name, expected in required.items():
        if values.get(name) and values[name] != expected:
            raise SetupError('Inconsistent embedder selection; reselect the pinned model.')
    missing = {name: value for name, value in required.items() if not values.get(name)}
    if missing:
        # Preserve URL, SHA, comments and unknown fields verbatim. Drop empty
        # identity assignments so the refreshed file has one value per field.
        kept = [line for line in original.splitlines() if line.partition('=')[0] not in missing]
        content = '\n'.join(kept) + ('\n' if kept else '')
        content += ''.join(f'{name}="{value}"\n' for name, value in missing.items())
        atomic_write(path, content, path.stat().st_mode & 0o777 if path.exists() else 0o644)
    atomic_write(DROPIN, '[Unit]\nConditionPathExists=\nConditionPathExists=' +
                 required['EMBEDDER_PATH'] + '\n')
    # A guest without downloaded weights remains in setup-needed state. Do not
    # turn systemd's skipped file condition into an upgrade activation failure.
    return ['bob-embedder.service'] if Path(required['EMBEDDER_PATH']).is_file() else []


def atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SetupError('Unsafe embedder storage directory.')
    fd, temporary = tempfile.mkstemp(prefix='.embedder-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            if os.name != 'nt':
                os.fchown(stream.fileno(), 0, 0)
                os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            parent = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def selection_env(key, model, *, origin='operator'):
    values = dict(EMBEDDER_MODEL=key, EMBEDDER_URL=model['url'], EMBEDDER_SHA256=model['sha256'],
                  EMBEDDER_DIM=str(model['dimension']), EMBEDDER_ALIAS=model['alias'],
                  EMBEDDER_PATH=str(DATA / 'models' / model['filename']))
    if origin is not None:
        values['EMBEDDER_SELECTION_ORIGIN'] = origin
    return ''.join(f'{name}="{value}"\n' for name, value in values.items())


def publish_selection(key, model, *, origin='operator'):
    # systemd cannot expand EnvironmentFile variables in ConditionPathExists.
    atomic_write(DROPIN, '[Unit]\nConditionPathExists=\nConditionPathExists=' +
                 str(DATA / 'models' / model['filename']) + '\n')
    atomic_write(CONFIG / 'embedder.env', selection_env(key, model, origin=origin))


def configure_build(env):
    """Build gate: reject placeholder/invalid pins before shipping either option."""
    values = {key: dict(value) for key, value in MODELS.items()}
    for key, prefix in (('4b', 'EMBEDDER'), ('0.6b', 'EMBEDDER_SMALL')):
        values[key].update(url=env.get(prefix + '_URL', values[key]['url']),
                           sha256=env.get(prefix + '_SHA256', values[key]['sha256']))
        checked_pin(values[key])
    atomic_write(CONFIG / 'embedder-models.json', json.dumps(values, indent=2) + '\n')
    if not (CONFIG / 'embedder.env').exists():
        publish_selection('4b', values['4b'], origin='build-default')
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)


def run(*args, **kw):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=kw.pop('timeout', 30), **kw)


def systemctl(*args):
    result = run('systemctl', *args)
    if result.returncode:
        raise SetupError('Embedder service operation failed; inspect the runtime journal.')
    return result.stdout.strip()


def http_json(path, *, port=6353):
    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}{path}', timeout=3) as response:
        return json.loads(response.read(1024 * 1024))


def loaded_alias(model):
    """Health alone is also green for a modelless llama-server."""
    try:
        response = http_json('/v1/models', port=58081)
        return any(isinstance(row, dict) and row.get('id') == model['alias']
                   for row in response['data'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def refuse_existing_vectors():
    try:
        rows = http_json('/collections')['result']['collections']
        if not isinstance(rows, list):
            raise ValueError()
        for row in rows:
            info = http_json('/collections/' + quote(row['name'], safe=''))['result']
            count = info['points_count']
            if type(count) is not int or count < 0:
                raise ValueError()
            if count:
                raise SetupError('Cannot switch embedder: existing memory vectors require re-embedding. '
                                 'Keep the current model; automatic re-embedding is not available.')
    except SetupError:
        raise
    except Exception:
        raise SetupError('Cannot switch embedder: vector state could not be verified. Start Bob Qdrant and retry.') from None


@contextmanager
def lock(*, nonblocking=False):
    import fcntl
    with open(LOCK, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            raise SetupError('An embedder install is already running. Check status.') from None
        yield


def progress(model, phase, detail):
    atomic_write(STATE / 'setup.json', json.dumps(dict(model=model, phase=phase, detail=detail, updated_at=time.time())) + '\n')


def file_identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def verified_file(model, *, verify=False):
    path = Path(model['path'])
    if not path.is_file() or path.is_symlink():
        return False
    if verify:
        ok = sha_ok(model)
        if ok:
            atomic_write(STATE / ('verified-' + model['alias'] + '.json'), json.dumps(dict(
                path=str(path), identity=file_identity(path), sha256=model['sha256'])) + '\n')
        return ok
    try:
        receipt = json.loads(read_public(STATE / ('verified-' + model['alias'] + '.json')))
        return (receipt['path'] == str(path) and receipt['sha256'] == model['sha256']
                and receipt['identity'] == file_identity(path))
    except (OSError, ValueError, KeyError):
        return False


def sha_ok(model):
    path = Path(model['path'])
    if not path.is_file() or path.is_symlink():
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == model['sha256']


def total_ram_gb():
    try:
        fields = Path('/proc/meminfo').read_text().splitlines()
        return round(int(next(line.split()[1] for line in fields if line.startswith('MemTotal:'))) / 1024**2, 1)
    except (OSError, ValueError, StopIteration):
        return None


def status(*, verify=True):
    model = selection()
    path = Path(model['path'])
    active = run('systemctl', 'is-active', 'bob-embedder.service').stdout.strip()
    health = None
    if path.is_file() and active in {'active', 'activating', 'reloading'}:
        try:
            health = http_json('/health', port=58081)
        except Exception:
            health = {'status': 'unavailable'}
    setup = json.loads(read_public(STATE / 'setup.json')) if (STATE / 'setup.json').exists() else None
    if setup and setup['phase'] in {'queued', 'downloading', 'verified', 'starting'}:
        job = run('systemctl', 'is-active', f'bob-embedder-install@{setup["model"]}.service').stdout.strip()
        if job not in {'active', 'activating', 'reloading'} and time.time() - setup.get('updated_at', 0) > 10:
            setup = {**setup, 'phase': 'failed', 'detail': 'Embedder install stopped. Check runtime services and retry.'}
    ram = total_ram_gb()
    # Frequent progress reads must not hash multi-GB files. Explicit CLI status does.
    digest = verified_file(model, verify=verify)
    model_loaded = loaded_alias(model) if isinstance(health, dict) and health.get('status') == 'ok' else False
    # Only a known image default without installed weights is genuinely fresh.
    installed = [key for key, value in catalog().items() if (DATA / 'models' / value['filename']).is_file()]
    configured = bool(installed) or model['origin'] != 'build-default'
    recommended = model['model'] if configured else '0.6b'
    ready = (active == 'active' and isinstance(health, dict)
             and health.get('status') == 'ok' and digest and model_loaded)
    return dict(selected_model=model['model'], model_path=model['path'], alias=model['alias'],
                embedding_dimension=model['dimension'], file_present=path.is_file(), sha_ok=digest,
                service_active=active == 'active', health=health, model_loaded=model_loaded, ready=ready, setup=setup,
                ram_gb=ram, recommended_model=recommended, configured=configured, installed_models=installed,
                options=[dict(model=key, size_gb=value['size_gb'], dimension=value['dimension'])
                         for key, value in catalog().items()])


ACTIVE = {'active', 'activating', 'reloading'}


def active_lane_services():
    """(cores, dependents) active now. Gateway/bridge Require their core, so stopping a
    core also stops them; only restore units that were active, never disabled lanes."""
    cores, dependents = [], []
    family = DATA / 'family'
    for path in sorted(family.iterdir()) if family.exists() else []:
        if not re.fullmatch('[a-z][a-z0-9-]{0,31}', path.name):
            continue
        core = 'bob-core@' + path.name + '.service'
        if run('systemctl', 'is-active', core).stdout.strip() not in ACTIVE:
            continue
        cores.append(core)
        for kind in ('gateway', 'bridge'):
            name = f'bob-{kind}@{path.name}.service'
            if run('systemctl', 'is-active', name).stdout.strip() in ACTIVE:
                dependents.append(name)
    return cores, dependents


def active_cores():
    return active_lane_services()[0]


def restore_services(cores, dependents):
    """Attempt the finite snapshot, each start bounded by run's timeout.

    Defer catchable stop signals until every unit has had its attempt. SIGKILL
    and host loss cannot be recovered here. Never infer additional lane units.
    """
    failed, cancellation = [], None
    def defer(signum, frame):
        nonlocal cancellation
        cancellation = cancellation or (Cancelled() if signum == signal.SIGTERM else KeyboardInterrupt())
    previous = {sig: signal.signal(sig, defer) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for unit in [*cores, *dependents]:
            try:
                systemctl('start', unit)
            except (Cancelled, KeyboardInterrupt) as exc:
                cancellation = cancellation or exc
                failed.append(unit)
            except (SetupError, OSError, subprocess.SubprocessError):
                failed.append(unit)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return failed, cancellation


def select(key, *, restart=True):
    # Install capture before any service can be stopped. Keep it through the
    # entire rollback/restoration call, including its entry and return: a local
    # cleanup handler alone leaves both transitions interruptible.
    cancellation = None
    def defer(signum, frame):
        nonlocal cancellation
        cancellation = cancellation or (Cancelled() if signum == signal.SIGTERM else KeyboardInterrupt())
    def checkpoint():
        if cancellation is not None:
            raise cancellation
    signals = (signal.SIGTERM, signal.SIGINT)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    previous = {sig: signal.signal(sig, defer) for sig in signals}
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        try:
            _select(key, restart=restart, checkpoint=checkpoint)
        except BaseException as exc:
            if cancellation is not None and exc is not cancellation:
                exc.restore_detail = (getattr(exc, 'restore_detail', '') +
                                      ' Cancellation was also requested during service restoration.').strip()
            raise
        checkpoint()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _select(key, *, restart, checkpoint):
    if key not in MODELS:
        raise SetupError('Choose 4b or 0.6b.')
    old = selection()
    # Rollback must retain legacy metadata, comments, extra fields and modes too.
    saved = [(path, read_public(path), path.stat().st_mode & 0o777)
             if path.exists() else (path, None, None)
             for path in (CONFIG / 'embedder.env', DROPIN)]
    cores, dependents = [], []
    publishing = False
    failure = None
    try:
        checkpoint()
        if key != old['model']:
            cores, dependents = active_lane_services()
            checkpoint()
            if cores:
                systemctl('stop', *dependents, *cores)
            checkpoint()
            refuse_existing_vectors()
            checkpoint()
        publishing = True
        publish_selection(key, catalog()[key])
        checkpoint()
        systemctl('daemon-reload')
        checkpoint()
        if restart:
            systemctl('restart', 'bob-embedder.service')
        checkpoint()
    except BaseException as exc:  # includes cancellation (SIGTERM/KeyboardInterrupt)
        failure = exc
        if publishing:
            try:
                for path, content, mode in saved:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, content, mode)
                systemctl('daemon-reload')
                systemctl('restart', 'bob-embedder.service')
            except (Exception, Cancelled, KeyboardInterrupt):
                pass  # the original failure stays authoritative; services are restored below
    finally:
        failed, cancellation = restore_services(cores, dependents)
    if failure is not None and cancellation is not None:
        failure.restore_detail = 'Cancellation was also requested during service restoration.'
    failure = failure or cancellation
    if failed:
        message = 'Services could not be restored: ' + ', '.join(failed) + '. Start them from the runtime before retrying.'
        if failure is not None:
            # Keep the original error/cancel identity and carry cleanup evidence
            # to the job receipt, rather than replacing cancellation with success.
            failure.restore_detail = (getattr(failure, 'restore_detail', '') + ' ' + message).strip()
            failure.add_note(message)
        else:
            raise SetupError(message)
    if failure is not None:
        raise failure


def fetch(key):
    if key != selection()['model']:
        # Repeat this check with writers stopped in select() before publication.
        refuse_existing_vectors()
    model = dict(catalog()[key])
    model['path'] = str(DATA / 'models' / model['filename'])
    directory = DATA / 'models'
    import pwd
    group = pwd.getpwnam('bob-embedder').pw_gid
    directory.mkdir(exist_ok=True)
    if directory.is_symlink():
        raise SetupError('Unsafe embedder model directory.')
    # The service may read weights, but must never replace root-verified files.
    os.chown(directory, 0, group)
    os.chmod(directory, 0o750)
    progress(key, 'downloading', 'Downloading the pinned model from Hugging Face…')
    if not sha_ok(model):
        fd, temporary = tempfile.mkstemp(prefix='.embedding-', dir=directory)
        os.close(fd)
        try:
            result = run('/usr/local/bin/fetch-checked', model['url'], model['sha256'], temporary, timeout=1800)
            if result.returncode or not sha_ok({**model, 'path': temporary}):
                raise SetupError('Model download or SHA256 verification failed. Retry the install.')
            os.chown(temporary, 0, group)
            os.chmod(temporary, 0o640)
            os.replace(temporary, model['path'])
        finally:
            Path(temporary).unlink(missing_ok=True)
    progress(key, 'verified', 'SHA256 verified. Selecting the embedder…')
    os.chown(model['path'], 0, group)
    os.chmod(model['path'], 0o640)
    verified_file(model, verify=True)
    select(key)
    progress(key, 'starting', 'Loading the embedder into memory…')
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if status(verify=False)['ready']:
            progress(key, 'ready', 'Embedder ready. Semantic memory is enabled.')
            return
        time.sleep(2)
    raise SetupError('Embedder did not become healthy within 3 minutes. Check the runtime journal and RAM.')


PENDING = {'queued', 'downloading', 'verified', 'starting'}


def install_in_progress():
    """A running install unit, or a fresh pending record covering the --no-block start window."""
    for model in MODELS:
        state = run('systemctl', 'is-active', f'bob-embedder-install@{model}.service').stdout.strip()
        if state in {'active', 'activating', 'reloading', 'deactivating'}:
            return True
    try:
        setup = json.loads(read_public(STATE / 'setup.json')) if (STATE / 'setup.json').exists() else None
    except (OSError, ValueError, SetupError):
        return False
    return bool(setup and setup.get('phase') in PENDING and time.time() - setup.get('updated_at', 0) <= 10)


def settings_operation(op, key=None):
    if op == 'embedder.status' and key is None:
        return status(verify=False)
    if op != 'embedder.install' or key not in MODELS:
        raise SetupError('Unsupported embedder operation or model.')
    with lock(nonblocking=True):
        if install_in_progress():
            raise SetupError('An embedder install is already running. Check status.')
        progress(key, 'queued', 'Embedder installation queued…')
        try:
            systemctl('start', '--no-block', f'bob-embedder-install@{key}.service')
        except Exception:
            progress(key, 'failed', 'Embedder install could not start. Check runtime services and retry.')
            raise
    return status(verify=False)


def run_job(command, key):
    """Run fetch/select with cancellation (SIGTERM) reported as an honest failed phase."""
    import signal

    def cancel(signum, frame):
        raise Cancelled()
    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        try:
            fetch(key) if command == 'fetch' else select(key)
        except (Cancelled, KeyboardInterrupt) as exc:
            detail = 'Embedder install was cancelled. Check Memory for the current model and runtime service state before retrying.'
            detail += ' ' + getattr(exc, 'restore_detail', '')
            progress(key, 'failed', detail)
            raise SetupError(detail) from None
        except Exception as exc:
            detail = str(exc) if isinstance(exc, SetupError) else 'Embedder setup failed; inspect runtime services.'
            progress(key, 'failed', detail + ' ' + getattr(exc, 'restore_detail', ''))
            raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    for name in ('fetch', 'select'):
        command = sub.add_parser(name)
        command.add_argument('--model', choices=MODELS, default=None, required=name == 'select')
    command = sub.add_parser('settings')
    command.add_argument('operation', choices=['embedder.status', 'embedder.install'])
    command.add_argument('--model', choices=MODELS)
    args = parser.parse_args()
    from common import require_root
    require_root()
    try:
        if args.command == 'status':
            result = status()
        elif args.command == 'settings':
            result = settings_operation(args.operation, args.model)
        else:
            import fcntl
            from lane import LOCK_PATH
            # Same allocation lock/order as lane creation and migration.
            with open(LOCK_PATH, 'a') as allocation:
                fcntl.flock(allocation, fcntl.LOCK_EX)
                with lock():
                    run_job(args.command, args.model or selection()['model'])
            result = status(verify=False)
        print(json.dumps(result), flush=True)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, SetupError) else 'Embedder setup failed; inspect runtime services.'}), flush=True)
        return 1
