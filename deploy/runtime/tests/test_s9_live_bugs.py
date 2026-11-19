"""S9 offline migration, rollback and model-readiness contracts."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

RUNTIME = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tmp_path(tmp_path_factory):
    if os.name != 'nt':
        return tmp_path_factory.mktemp('s9')
    path = RUNTIME.parents[1] / '.s9-testdata' / uuid.uuid4().hex
    path.mkdir(mode=0o777, parents=True)
    return path


@pytest.fixture
def helper(tmp_path, monkeypatch):
    module = load('s9_embedder', RUNTIME / 'lib/embedder.py')
    for name, value in dict(CONFIG=tmp_path / 'etc', DATA=tmp_path / 'data', STATE=tmp_path / 'state',
                            DROPIN=tmp_path / 'unit/selection.conf').items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='active'))
    monkeypatch.setattr(module, 'total_ram_gb', lambda: 16)
    module.CONFIG.mkdir(mode=0o777)
    return module


@pytest.mark.parametrize('key', ['4b', '0.6b', 'unknown'])
@pytest.mark.parametrize('present', [False, True])
def test_c4_env_migration_is_complete_atomic_idempotent_and_preserves_pins(helper, key, present):
    m = helper
    chosen = '4b' if key == 'unknown' else key
    model = m.MODELS[chosen]
    url = model['url'] if key != 'unknown' else 'https://example.test/old.gguf'
    original = '# preserved\nEMBEDDER_URL="' + url + '"\nEMBEDDER_SHA256="' + model['sha256'] + '"\nEXTRA=keep\n'
    path = m.CONFIG / 'embedder.env'
    path.write_text(original, encoding='utf-8')
    if present:
        weights = m.DATA / 'models' / model['filename']
        weights.parent.mkdir(mode=0o777, parents=True)
        weights.write_bytes(b'fixture')
    assert m.migrate_selection() == (['bob-embedder.service'] if present else [])
    result = path.read_text()
    assert result.startswith(original)
    assert f'EMBEDDER_MODEL="{chosen}"' in result
    assert f'EMBEDDER_ALIAS="{model["alias"]}"' in result
    assert f'EMBEDDER_DIM="{model["dimension"]}"' in result
    assert f'EMBEDDER_PATH="{m.DATA / "models" / model["filename"]}"' in result
    assert str(m.DATA / 'models' / model['filename']) in m.DROPIN.read_text()
    m.migrate_selection()
    assert path.read_text() == result
    assert not list(m.CONFIG.glob('.embedder-*'))
    if os.name != 'nt':
        assert path.stat().st_uid == 0 and path.stat().st_mode & 0o777 == 0o644


def test_migration_recreates_dropin_after_interrupted_publication(helper):
    m = helper
    m.atomic_write(m.CONFIG / 'embedder.env', m.selection_env('0.6b', m.MODELS['0.6b']), 0o600)
    m.migrate_selection()
    assert m.selection()['model'] == '0.6b'
    assert m.MODELS['0.6b']['filename'] in m.DROPIN.read_text()
    if os.name != 'nt':
        assert (m.CONFIG / 'embedder.env').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('models,ready', [([], False), ([{'id': 'wrong-model'}], False),
                                        ([{'id': 'qwen3-embedding-4b'}], True), (None, False)])
def test_status_and_health_require_selected_alias(helper, monkeypatch, models, ready):
    m = helper
    m.publish_selection('4b', m.MODELS['4b'])
    model = Path(m.selection()['path'])
    model.parent.mkdir(mode=0o777, parents=True)
    model.write_bytes(b'offline-fixture')
    monkeypatch.setattr(m, 'verified_file', lambda *a, **kw: True)
    monkeypatch.setattr(m, 'http_json', lambda path, **kw: {'status': 'ok'} if path == '/health' else {'data': models})
    assert m.status()['ready'] is ready
    monkeypatch.setitem(sys.modules, 'embedder', m)
    monkeypatch.setitem(sys.modules, 'common', SimpleNamespace(read_env=None, require_root=None))
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(DATA=m.DATA, lanes=None, units=None))
    health = load('s9_health', RUNTIME / 'lib/health.py')
    monkeypatch.setattr(health, 'endpoint', lambda port: {'healthy': True, 'body': {'status': 'ok'}})
    result = health.embedder_status({'ActiveState': 'active', 'query_ok': True})
    assert result['ready'] is ready
    assert result['status'] == ('ready' if ready else 'failed')


def test_upgrade_snapshots_and_restores_env_and_dropin(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(RUNTIME / 'lib'))
    import runtime_upgrade as upgrade
    actual_path = Path
    def mapped(name):
        name = str(name)
        return tmp_path / name.lstrip('/') if name.startswith('/etc/') else actual_path(name)
    monkeypatch.setattr(upgrade, 'Path', mapped)
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(lanes=lambda: []))
    monkeypatch.setitem(sys.modules, 'worker', SimpleNamespace(load_identity=None))
    folder = tmp_path / 'journal'
    folder.mkdir(mode=0o777)
    path = mapped('/etc/bob/embedder.env')
    path.parent.mkdir(mode=0o777, parents=True)
    path.write_text('EMBEDDER_URL=old\nEMBEDDER_SHA256=keep\n')
    ops = upgrade.LinuxOperations()
    ops.backup_controls(folder)
    receipt = json.loads((folder / 'embedder-controls.json').read_text())
    assert receipt['/etc/bob/embedder.env']['content'] == path.read_text()
    path.write_text('EMBEDDER_PATH=new\n')
    dropin = mapped('/etc/systemd/system/bob-embedder.service.d/selection.conf')
    dropin.parent.mkdir(mode=0o777, parents=True)
    dropin.write_text('new')
    ops.restore_controls(folder, {})
    assert path.read_text() == 'EMBEDDER_URL=old\nEMBEDDER_SHA256=keep\n'
    assert not dropin.exists()


def test_hook_enqueues_embedder_for_activation(monkeypatch):
    monkeypatch.syspath_prepend(str(RUNTIME / 'lib'))
    if os.name == 'nt':
        monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace())
    hooks = load('s9_hooks', RUNTIME / 'lib/upgrade_hooks.py')
    monkeypatch.setitem(sys.modules, 'embedder', SimpleNamespace(migrate_selection=lambda: ['bob-embedder.service']))
    assert dict(hooks.HOOKS)['c5-embedder-env']() == ['bob-embedder.service']
    import runtime_upgrade as upgrade
    ops = upgrade.LinuxOperations()
    monkeypatch.setattr(ops, 'runtime', lambda code: SimpleNamespace(stdout=b'{"units":["bob-embedder.service"]}'))
    state = {'start_new': []}
    saves = []
    ops.hooks(None, state, lambda: saves.append(list(state['start_new'])))
    assert saves == [['bob-embedder.service']]


def test_unit_and_backend_check_refuse_modelless_service():
    unit = (RUNTIME / 'systemd/bob-embedder.service').read_text()
    assert 'ExecStartPre=' in unit
    assert 'test -n "$${EMBEDDER_PATH}"' in unit and 'test -n "$${EMBEDDER_ALIAS}"' in unit
    check = (RUNTIME / 'check-embedder-backend.sh').read_text()
    assert 'loaded_alias(model)' in check and 'absent from /v1/models' in check


@pytest.mark.parametrize('alias,dimension,ok', [('qwen3-embedding-4b', 2560, True),
                                              ('wrong-model', 2560, False), ('qwen3-embedding-4b', 1024, False)])
def test_acceptance_probes_alias_and_embedding_dimension(monkeypatch, alias, dimension, ok):
    acceptance = load('s9_acceptance', RUNTIME / 'tests/builder_acceptance.py')
    responses = [dict(data=[{'id': alias}]), dict(data=[{'embedding': [1.0] * dimension}])]
    class Connection:
        def __init__(self, *a, **kw): pass
        def request(self, method, path, **kw):
            assert path in {'/v1/models', '/v1/embeddings'}
        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda size: json.dumps(responses.pop(0)).encode())
        def close(self): pass
    monkeypatch.setattr(acceptance, 'HTTPConnection', Connection)
    status = {'alias': 'qwen3-embedding-4b', 'embedding_dimension': 2560}
    if ok:
        acceptance.embedder_probe(status)
    else:
        with pytest.raises(AssertionError):
            acceptance.embedder_probe(status)
