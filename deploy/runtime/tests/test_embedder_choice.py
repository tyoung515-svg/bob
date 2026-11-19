"""Offline S4 setup contracts. No downloads, provider calls, or live services."""
import asyncio
from contextlib import nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
ROOT = RUNTIME.parents[1]
sys.path.insert(0, str(RUNTIME / 'lib'))
sys.path.insert(0, str(ROOT / 'bobclaw-core'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper(tmp_path, monkeypatch):
    module = load('s4_embedder', RUNTIME / 'lib/embedder.py')
    for name, value in dict(CONFIG=tmp_path / 'etc', DATA=tmp_path / 'data', STATE=tmp_path / 'state',
                            DROPIN=tmp_path / 'unit/selection.conf').items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='inactive'))
    monkeypatch.setattr(module, 'total_ram_gb', lambda: 8)
    module.configure_build({})
    return module


@pytest.mark.parametrize('key,dimension,filename', [
    ('4b', 2560, 'Qwen3-Embedding-4B-Q4_K_M.gguf'),
    ('0.6b', 1024, 'Qwen3-Embedding-0.6B-Q8_0.gguf'),
])
def test_selection_persists_complete_env_and_selected_file_condition(helper, key, dimension, filename):
    m = helper
    m.publish_selection(key, m.catalog()[key])
    selection = m.selection()
    assert selection['model'] == key and selection['dimension'] == dimension
    env = (m.CONFIG / 'embedder.env').read_text()
    for field in ('MODEL', 'URL', 'SHA256', 'DIM', 'ALIAS', 'PATH'):
        assert 'EMBEDDER_' + field + '=' in env
    assert filename in m.DROPIN.read_text()
    assert 'ConditionPathExists=\n' in m.DROPIN.read_text()
    assert not list(m.CONFIG.glob('.embedder-*'))
    if sys.platform != 'win32':
        assert (m.CONFIG / 'embedder.env').stat().st_uid == 0


@pytest.mark.parametrize('digest', ['REPLACE_ME', 'UNVERIFIED', '', '0' * 64, 'a' * 63])
def test_build_refuses_unverified_small_digest_before_publication(helper, digest):
    original = (helper.CONFIG / 'embedder-models.json').read_bytes()
    with pytest.raises(helper.SetupError, match='Unverified'):
        helper.configure_build({'EMBEDDER_SMALL_SHA256': digest})
    assert (helper.CONFIG / 'embedder-models.json').read_bytes() == original


@pytest.mark.parametrize('url', ['https://evil.test/model.gguf', 'http://huggingface.co/Qwen/m.gguf',
                                'https://huggingface.co.evil/Qwen/m.gguf',
                                'https://huggingface.co/Qwen/x/resolve/main/a.gguf\nINJECT=bad'])
def test_build_refuses_non_official_or_injected_urls(helper, url):
    with pytest.raises(helper.SetupError, match='Unverified'):
        helper.configure_build({'EMBEDDER_SMALL_URL': url})


def test_unit_uses_selection_not_a_hardcoded_model():
    unit = (RUNTIME / 'systemd/bob-embedder.service').read_text()
    assert 'EnvironmentFile=/etc/bob/embedder.env' in unit
    assert '--model ${EMBEDDER_PATH} --alias ${EMBEDDER_ALIAS}' in unit
    assert '--host 127.0.0.1 --port 58081' in unit
    assert 'Qwen3-Embedding-4B-Q4_K_M.gguf' not in unit


@pytest.mark.parametrize('key,dimension', [('4b', 2560), ('0.6b', 1024)])
def test_slots_and_collection_suffix_follow_selection(helper, monkeypatch, key, dimension):
    from core.memory import slots
    from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
    helper.publish_selection(key, helper.catalog()[key])
    selection = helper.CONFIG / 'embedder.env'
    parsed = slots._runtime_embedding_selection(selection)
    monkeypatch.setattr(slots, '_runtime_embedding_selection', lambda path: parsed)
    resolver = slots.SlotResolver(RUNTIME / 'config/memory_slots.toml')
    for name in ('embed_text', 'embed_text_heavy'):
        resolved = resolver.get(name)
        assert resolved.model == 'qwen3-embedding-' + key
        assert resolved.embedding_dimension == dimension
        assert resolved.endpoint == 'http://127.0.0.1:58081'
    provider = object.__new__(QdrantRetrievalProvider)
    provider.collection_prefix = 'travis_bob'
    assert provider._collection_name(dimension) == f'travis_bob_{dimension}'
    assert resolver.keyword_fallback


@pytest.mark.parametrize('count', [1, 50, None, -1, True])
def test_switch_refuses_existing_or_unverifiable_vectors_and_restores_cores(helper, monkeypatch, count):
    m = helper
    monkeypatch.setattr(m, 'active_lane_services', lambda: (['bob-core@alice.service'], []))
    monkeypatch.setattr(m, 'http_json', lambda path, **kw: dict(result=dict(collections=[dict(name='alice_bob_2560')]))
                        if path == '/collections' else dict(result=dict(points_count=count)))
    calls = []
    monkeypatch.setattr(m, 'systemctl', lambda *a: calls.append(a))
    with pytest.raises(m.SetupError, match='Cannot switch embedder'):
        m.select('0.6b')
    assert calls == [('stop', 'bob-core@alice.service'), ('start', 'bob-core@alice.service')]
    assert m.selection()['model'] == '4b'


def test_empty_vectors_allow_switch_and_same_selection_needs_no_vector_probe(helper, monkeypatch):
    m = helper
    monkeypatch.setattr(m, 'http_json', lambda *a, **kw: dict(result=dict(collections=[])))
    calls = []
    monkeypatch.setattr(m, 'systemctl', lambda *a: calls.append(a))
    m.select('0.6b')
    assert m.selection()['dimension'] == 1024
    assert calls == [('daemon-reload',), ('restart', 'bob-embedder.service')]
    monkeypatch.setattr(m, 'http_json', lambda *a, **kw: pytest.fail('same model must not inspect vectors'))
    m.select('0.6b')


def test_status_sha_is_real_and_cached_receipt_detects_modified_bytes(helper, monkeypatch):
    m = helper
    data = b'test-model-not-a-real-GGUF'
    pins = {key: dict(value) for key, value in m.MODELS.items()}
    pins['0.6b']['sha256'] = hashlib.sha256(data).hexdigest()
    m.atomic_write(m.CONFIG / 'embedder-models.json', json.dumps(pins))
    m.publish_selection('0.6b', pins['0.6b'])
    path = Path(m.selection()['path'])
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    monkeypatch.setattr(m, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='active'))
    monkeypatch.setattr(m, 'http_json', lambda path, **kw: {'status': 'ok'} if path == '/health'
                        else {'data': [{'id': m.selection()['alias']}]})
    assert m.status()['sha_ok'] is True
    assert m.status(verify=False)['ready'] is True
    path.write_bytes(b'changed')
    assert m.status(verify=False)['sha_ok'] is False
    assert m.status()['ready'] is False


def test_settings_operations_are_bounded_nonblocking_and_admit_no_urls(helper, monkeypatch):
    from core.settings_contract import validate, SettingsError
    m = helper
    calls = []
    monkeypatch.setattr(m, 'lock', lambda **kw: nullcontext())
    monkeypatch.setattr(m, 'systemctl', lambda *a: calls.append(a))
    result = m.settings_operation('embedder.install', '0.6b')
    assert result['setup']['phase'] == 'queued'
    assert calls == [('start', '--no-block', 'bob-embedder-install@0.6b.service')]
    validate(dict(version=1, op='embedder.status'))
    validate(dict(version=1, op='embedder.install', model='0.6b'))
    for request in [dict(version=1, op='embedder.install', model='evil'),
                    dict(version=1, op='embedder.install', model='4b', url='https://evil'),
                    dict(version=1, op='embedder.status', model='4b')]:
        with pytest.raises(SettingsError):
            validate(request)
    with pytest.raises(SettingsError, match='unsupported_operation'):
        validate(dict(version=1, op='embedder.install', model='4b'), receipt=True)


def test_health_reports_selected_model_and_missing_setup(helper, monkeypatch):
    monkeypatch.setitem(sys.modules, 'common', SimpleNamespace(read_env=lambda p: {}, require_root=lambda: None))
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(DATA=helper.DATA, lanes=lambda: [], units=lambda n: []))
    monkeypatch.setitem(sys.modules, 'embedder', helper)
    m = load('s4_health', RUNTIME / 'lib/health.py')
    helper.publish_selection('0.6b', helper.catalog()['0.6b'])
    result = m.embedder_status(dict(ActiveState='inactive', query_ok=True))
    assert result['selected_model'] == '0.6b' and result['embedding_dimension'] == 1024
    assert result['model_path'].endswith('Qwen3-Embedding-0.6B-Q8_0.gguf')
    assert result['status'] == 'setup-needed'


def test_small_acceptance_requires_1024_finite_distinct_vectors():
    acceptance = load('s4_acceptance', RUNTIME / 'tests/embedder_acceptance.py')
    vectors = [[1.0] * 1024, [2.0] * 1024]
    assert acceptance.vector_receipt(vectors, 1024)['valid']
    assert not acceptance.vector_receipt(vectors, 2560)['valid']
    assert not acceptance.vector_receipt([[0.0] * 1024] * 2, 1024)['valid']


def test_settings_socket_checks_peer_and_receipt_scope_before_root_action(helper, monkeypatch):
    from core.settings_contract import SettingsError
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'common', SimpleNamespace(
        SYSTEMD=helper.DROPIN.parent, private_replace=None, require_root=None,
        root_directory=None, writer_guard=lambda lane: None))
    monkeypatch.setitem(sys.modules, 'embedder', helper)
    settings = load('s4_settings', RUNTIME / 'lib/settings.py')
    cfg = dict(lane='test', gateway_uid=1001, broker_uid=1002)
    calls = []
    monkeypatch.setattr(helper, 'settings_operation', lambda *a: calls.append(a) or {'ready': False})
    request = dict(version=1, op='embedder.install', model='0.6b')
    for uid in (0, 999, 1002, 1003):
        with pytest.raises(SettingsError, match='peer_denied'):
            settings.handle(cfg, uid, request)
    with pytest.raises(SettingsError, match='unsupported_operation'):
        settings.handle(cfg, 1002, request, readonly=True)
    assert calls == []
    assert settings.handle(cfg, 1001, request) == {'ready': False}
    assert calls == [('embedder.install', '0.6b')]


@pytest.mark.parametrize('valid', [False, True])
def test_fetch_verifies_before_publication_and_cleans_failed_temporary(helper, monkeypatch, valid):
    m = helper
    data = b'offline-placeholder-weights'
    pins = {key: dict(value) for key, value in m.MODELS.items()}
    pins['0.6b']['sha256'] = hashlib.sha256(data).hexdigest()
    m.atomic_write(m.CONFIG / 'embedder-models.json', json.dumps(pins))
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_gid=99)))
    monkeypatch.setattr(m.os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(m.os, 'chmod', lambda *a: None)
    monkeypatch.setattr(m, 'refuse_existing_vectors', lambda: None)
    monkeypatch.setattr(m, 'status', lambda **kw: {'ready': True})
    calls = []
    def fetch(*args, **kw):
        assert args[0] == '/usr/local/bin/fetch-checked'
        assert args[1].startswith('https://huggingface.co/Qwen/')
        assert args[2] == pins['0.6b']['sha256']
        Path(args[3]).write_bytes(data if valid else b'bad-sha')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(m, 'run', fetch)
    monkeypatch.setattr(m, 'select', lambda key: calls.append(key))
    m.DATA.mkdir()
    if valid:
        m.fetch('0.6b')
        assert calls == ['0.6b']
        assert Path(m.DATA / 'models' / pins['0.6b']['filename']).read_bytes() == data
    else:
        with pytest.raises(m.SetupError, match='SHA256'):
            m.fetch('0.6b')
        assert calls == [] and m.selection()['model'] == '4b'
        assert not (m.DATA / 'models' / pins['0.6b']['filename']).exists()
    assert not list((m.DATA / 'models').glob('.embedding-*'))


def test_interrupted_install_status_is_terminal_without_restarting_services(helper, monkeypatch):
    helper.progress('0.6b', 'downloading', 'Downloading…')
    monkeypatch.setattr(helper.time, 'time', lambda: 99999999999)
    result = helper.status(verify=False)
    assert result['setup']['phase'] == 'failed'
    assert 'stopped' in result['setup']['detail']


def test_keyword_fallback_reads_only_t0_projection_surface_and_respects_filters(tmp_path):
    from core.memory.keyword import search
    path = tmp_path / 'memory.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE memory_facts(fact_id,body_json,confidence_json,ts); '
                         'CREATE TABLE memory_events(event_id,kind,body_json,ts,insertion_order);')
        db.execute('INSERT INTO memory_facts VALUES (?,?,?,?)',
                   ('gone', '{"text":"bicycle"}', '{"rank":"deprecated"}', '2026-01-01'))
        db.execute('INSERT INTO memory_events VALUES (?,?,?,?,?)', ('event1', 'agent_turn',
                   json.dumps(dict(assistant_response='A blue bicycle at the gate', private_field='SECRET')), '2026-01-01', 1))
        db.execute('INSERT INTO memory_events VALUES (?,?,?,?,?)', ('event2', 'agent_turn',
                   json.dumps(dict(assistant_response='Other response', private_field='bicycle SECRET')), '2026-01-02', 2))
    result = asyncio.run(search(path, 'bicycle', top_k=3, t0=True))
    assert len(result) == 1 and 'bicycle' in result[0].payload['text']
    assert 'SECRET' not in result[0].payload['text']
    assert asyncio.run(search(path, 'bicycle', top_k=3, t0=False)) == []
    assert asyncio.run(search(path, 'bicycle', top_k=3, filters={'owner': 'other'}, t0=True)) == []


def test_retriever_uses_keyword_fallback_only_for_runtime_opt_in(tmp_path):
    from unittest.mock import AsyncMock
    from core.memory.exceptions import EmbedderUnavailable
    from core.memory.fact_store import SQLiteFactStore
    from core.memory.retriever import MemoryRetriever
    path = tmp_path / 'recall.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE memory_facts(fact_id,body_json,confidence_json,ts); '
                         'CREATE TABLE memory_events(event_id,kind,body_json,ts,insertion_order);')
        db.execute('INSERT INTO memory_events VALUES (?,?,?,?,?)', ('t0-source', 'agent_turn',
                   json.dumps({'assistant_response': 'A bicycle by the gate'}), '2026-01-01', 1))
    embedder = SimpleNamespace(embed=AsyncMock(side_effect=EmbedderUnavailable('http://127.0.0.1:58081', 'not installed')))
    resolver = SimpleNamespace(get=lambda name: None, keyword_fallback=True)
    records = []
    retriever = MemoryRetriever(embedder, None, SQLiteFactStore(path), 'test_bob', resolver,
                                SimpleNamespace(append=records.append), t0_recall_enabled=True)
    result = asyncio.run(retriever.search('bicycle'))
    assert len(result) == 1 and result[0].source_path == 'event://t0-source'
    assert records == [{'mode': 'keyword-only', 'result_count': 1}]
    resolver.keyword_fallback = False
    with pytest.raises(EmbedderUnavailable):
        asyncio.run(retriever.search('bicycle'))


class FakeSystemd:
    """is-active/stop/start with systemd's Requires= cascade: stopping a core stops its
    gateway and bridge; starting a core does NOT start them again."""

    def __init__(self, active):
        self.active = set(active)
        self.log = []

    def run(self, *args, **kw):
        if args[:2] == ('systemctl', 'is-active'):
            state = 'active' if args[2] in self.active else 'inactive'
            return SimpleNamespace(returncode=0 if state == 'active' else 3, stdout=state)
        return SimpleNamespace(returncode=0, stdout='')

    def systemctl(self, *args):
        self.log.append(args)
        verb, units = args[0], args[1:]
        if verb == 'stop':
            for unit in units:
                self.active.discard(unit)
                if unit.startswith('bob-core@'):
                    lane = unit.split('@', 1)[1].removesuffix('.service')
                    self.active -= {f'bob-gateway@{lane}.service', f'bob-bridge@{lane}.service'}
        elif verb == 'start':
            self.active.update(units)


LANE_UNITS = {
    'alice': {'bob-core@alice.service', 'bob-gateway@alice.service', 'bob-bridge@alice.service'},
    # gateway deliberately stopped by the operator: must not be started by the embedder switch
    'bea': {'bob-core@bea.service', 'bob-bridge@bea.service'},
}


@pytest.fixture
def lifecycle(helper, monkeypatch):
    m = helper
    for lane in ('alice', 'bea', 'carol'):  # carol: intentionally disabled lane (inactive)
        (m.DATA / 'family' / lane).mkdir(parents=True)
    fake = FakeSystemd(set().union(*LANE_UNITS.values()))
    initial = set(fake.active)
    monkeypatch.setattr(m, 'run', fake.run)
    monkeypatch.setattr(m, 'systemctl', fake.systemctl)
    monkeypatch.setattr(m, 'http_json', lambda *a, **kw: dict(result=dict(collections=[])))
    return m, fake, initial


def test_switch_success_restores_every_active_lane_service_and_not_disabled_lanes(lifecycle):
    m, fake, initial = lifecycle
    m.select('0.6b')
    assert fake.active == initial
    assert not any('carol' in unit for unit in fake.active)
    assert m.selection()['model'] == '0.6b'


def test_switch_refusal_restores_every_active_lane_service(lifecycle, monkeypatch):
    m, fake, initial = lifecycle
    monkeypatch.setattr(m, 'http_json', lambda path, **kw: dict(result=dict(collections=[dict(name='alice_bob_2560')]))
                        if path == '/collections' else dict(result=dict(points_count=7)))
    with pytest.raises(m.SetupError, match='Cannot switch embedder'):
        m.select('0.6b')
    assert fake.active == initial and m.selection()['model'] == '4b'


def test_switch_cancellation_rolls_back_selection_and_restores_services(lifecycle, monkeypatch):
    m, fake, initial = lifecycle
    original = fake.systemctl
    def cancelled(*args):
        if args[:2] == ('restart', 'bob-embedder.service') and m.selection()['model'] == '0.6b':
            raise KeyboardInterrupt()
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', cancelled)
    with pytest.raises(KeyboardInterrupt):
        m.select('0.6b')
    assert fake.active == initial and m.selection()['model'] == '4b'


def test_one_failed_restore_does_not_skip_the_rest_and_is_reported(lifecycle, monkeypatch):
    m, fake, initial = lifecycle
    original = fake.systemctl
    def flaky(*args):
        if args[0] == 'start' and 'bob-core@alice.service' in args:
            raise m.SetupError('Embedder service operation failed; inspect the runtime journal.')
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', flaky)
    with pytest.raises(m.SetupError, match='could not be restored.*bob-core@alice'):
        m.select('0.6b')
    assert 'bob-core@bea.service' in fake.active and 'bob-bridge@bea.service' in fake.active
    assert 'bob-gateway@bea.service' not in fake.active


def test_same_model_selection_never_stops_services(lifecycle):
    m, fake, initial = lifecycle
    m.select('4b')
    assert fake.active == initial
    assert not any(call[0] == 'stop' for call in fake.log)


def test_duplicate_install_is_refused_while_a_fresh_queued_record_exists(helper, monkeypatch):
    m = helper
    calls = []
    monkeypatch.setattr(m, 'lock', lambda **kw: nullcontext())
    monkeypatch.setattr(m, 'systemctl', lambda *a: calls.append(a))
    # systemd reports the oneshot inactive during the --no-block start window.
    m.settings_operation('embedder.install', '0.6b')
    with pytest.raises(m.SetupError, match='already running'):
        m.settings_operation('embedder.install', '4b')
    assert calls == [('start', '--no-block', 'bob-embedder-install@0.6b.service')]


def test_old_failed_or_terminal_record_does_not_block_a_retry(helper, monkeypatch):
    m = helper
    calls = []
    monkeypatch.setattr(m, 'lock', lambda **kw: nullcontext())
    monkeypatch.setattr(m, 'systemctl', lambda *a: calls.append(a))
    m.progress('0.6b', 'failed', 'Model download failed.')
    m.settings_operation('embedder.install', '0.6b')
    m.progress('0.6b', 'ready', 'Embedder ready.')
    m.settings_operation('embedder.install', '0.6b')
    assert len(calls) == 2


def test_cancelled_job_records_an_honest_failed_phase_and_restores_services(lifecycle, monkeypatch):
    m, fake, initial = lifecycle
    monkeypatch.setattr(m, 'fetch', lambda key: (m.select(key), (_ for _ in ()).throw(m.Cancelled()))[1])
    with pytest.raises(m.SetupError, match='cancelled'):
        m.run_job('fetch', '0.6b')
    record = json.loads((m.STATE / 'setup.json').read_text())
    assert record['phase'] == 'failed' and 'cancelled' in record['detail'].lower()
    assert 'no semantic memory model is active' not in record['detail'].lower(), 'rollback may have restored a working model'
    assert fake.active == initial


def test_fresh_install_recommends_lightweight_regardless_of_ram_but_never_rewrites_existing(helper, monkeypatch):
    m = helper
    monkeypatch.setattr(m, 'total_ram_gb', lambda: 64)
    fresh = m.status(verify=False)
    assert fresh['recommended_model'] == '0.6b' and fresh['configured'] is False
    assert fresh['selected_model'] == '4b', 'the unconfigured image default is reported, not silently rewritten'
    # An installed 4B model stays recommended/selected even on a small machine.
    monkeypatch.setattr(m, 'total_ram_gb', lambda: 4)
    four = Path(m.selection()['path'])
    four.parent.mkdir(parents=True)
    four.write_bytes(b'weights')
    existing = m.status(verify=False)
    assert existing['configured'] is True and existing['recommended_model'] == '4b'
    assert existing['installed_models'] == ['4b']
    # The loaded-model facts the UI needs to tell download apart from loaded/ready.
    assert existing['model_loaded'] is False and existing['alias'] == 'qwen3-embedding-4b'


@pytest.mark.parametrize('kind', ['timeout', 'oserror', 'setup', 'cancel'])
def test_restore_exception_attempts_all_snapshot_units(lifecycle, monkeypatch, kind):
    import subprocess
    m, fake, initial = lifecycle
    original = fake.systemctl
    errors = {'timeout': subprocess.TimeoutExpired(['systemctl'], 30),
              'oserror': OSError('start failed'), 'setup': m.SetupError('start failed'),
              'cancel': m.Cancelled()}
    def start(*args):
        if args == ('start', 'bob-core@alice.service'):
            assert m.selection()['model'] == '0.6b'
            assert not fake.active
            raise errors[kind]
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', start)
    with pytest.raises(m.Cancelled if kind == 'cancel' else m.SetupError) as caught:
        m.select('0.6b')
    assert set(unit for call in fake.log if call[0] == 'start' for unit in call[1:]) == initial - {'bob-core@alice.service'}
    assert 'bob-core@bea.service' in fake.active and 'bob-bridge@bea.service' in fake.active
    if kind != 'cancel':
        assert 'bob-core@alice.service' in str(caught.value)


def test_missing_weight_operator_and_legacy_preference(helper):
    m = helper
    m.publish_selection('4b', m.catalog()['4b'])
    result = m.status(verify=False)
    assert result['configured'] and result['recommended_model'] == result['selected_model'] == '4b'
    env = m.CONFIG / 'embedder.env'
    m.atomic_write(env, '\n'.join(line for line in env.read_text().splitlines()
                                if not line.startswith('EMBEDDER_SELECTION_ORIGIN=')) + '\n')
    assert m.status(verify=False)['configured'] is True
    assert m.status(verify=False)['recommended_model'] == '4b'


def test_selection_failure_rolls_back_origin(lifecycle, monkeypatch):
    m, fake, initial = lifecycle
    before = (m.CONFIG / 'embedder.env').read_bytes()
    original = fake.systemctl
    def restart(*args):
        if args == ('restart', 'bob-embedder.service') and m.selection()['model'] == '0.6b':
            raise m.SetupError('original restart failed')
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', restart)
    with pytest.raises(m.SetupError, match='original restart failed'):
        m.select('0.6b')
    assert (m.CONFIG / 'embedder.env').read_bytes() == before
    assert m.status(verify=False)['configured'] is False
    assert fake.active == initial


@pytest.mark.parametrize('original_kind', ['cancel', 'error'])
def test_job_preserves_original_outcome_and_aggregates_restore_failures(lifecycle, monkeypatch, original_kind):
    m, fake, initial = lifecycle
    original = fake.systemctl
    failure = m.Cancelled() if original_kind == 'cancel' else m.SetupError('original publish restart failure')
    def operation(*args):
        if args == ('restart', 'bob-embedder.service') and m.selection()['model'] == '0.6b':
            raise failure
        if args in [('start', 'bob-core@alice.service'), ('start', 'bob-bridge@alice.service')]:
            raise OSError('failed restore')
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', operation)
    with pytest.raises(m.SetupError):
        m.run_job('select', '0.6b')
    detail = json.loads((m.STATE / 'setup.json').read_text())['detail']
    assert ('cancelled' if original_kind == 'cancel' else 'original publish restart failure') in detail
    assert 'bob-core@alice.service' in detail and 'bob-bridge@alice.service' in detail
    assert 'were restored' not in detail
    assert 'bob-bridge@bea.service' in fake.active
    assert fake.active <= initial


def test_sigterm_during_restore_is_deferred_and_handlers_restored(lifecycle, monkeypatch):
    import signal
    m, fake, initial = lifecycle
    original = fake.systemctl
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    def operation(*args):
        if args == ('start', 'bob-core@alice.service'):
            assert m.selection()['model'] == '0.6b' and not fake.active
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', operation)
    with pytest.raises(m.SetupError, match='cancelled'):
        m.run_job('select', '0.6b')
    assert fake.active == initial
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())


def test_explicit_same_selection_and_build_replay_preserve_preference(helper, monkeypatch):
    m = helper
    monkeypatch.setattr(m, 'systemctl', lambda *args: None)
    m.select('4b')
    m.configure_build({})
    assert m.selection()['origin'] == 'operator'
    assert m.status(verify=False)['configured'] is True
    assert m.status(verify=False)['recommended_model'] == '4b'


def test_missing_selected_weights_with_other_weights_still_preserves_preference(helper):
    m = helper
    m.publish_selection('4b', m.catalog()['4b'])
    other = m.DATA / 'models' / m.catalog()['0.6b']['filename']
    other.parent.mkdir(parents=True)
    other.write_bytes(b'other model')
    result = m.status(verify=False)
    assert result['configured'] and not result['file_present']
    assert result['selected_model'] == result['recommended_model'] == '4b'


@pytest.mark.parametrize('origin', ['operator', None])
def test_rollback_retains_operator_or_legacy_env_verbatim(lifecycle, monkeypatch, origin):
    m, fake, initial = lifecycle
    m.publish_selection('4b', m.catalog()['4b'], origin=origin)
    env = m.CONFIG / 'embedder.env'
    m.atomic_write(env, env.read_text() + '# retained operator field\nEXTRA="retained"\n', 0o600)
    before = env.read_bytes()
    original = fake.systemctl
    def restart(*args):
        if args == ('restart', 'bob-embedder.service') and m.selection()['model'] == '0.6b':
            raise m.SetupError('restart failed')
        return original(*args)
    monkeypatch.setattr(m, 'systemctl', restart)
    with pytest.raises(m.SetupError):
        m.select('0.6b')
    assert env.read_bytes() == before and env.stat().st_mode & 0o777 == 0o600
    assert m.selection()['origin'] == origin and m.status(verify=False)['configured']
    assert fake.active == initial


@pytest.mark.parametrize('original_error', [False, True])
def test_repeated_sigterm_at_cleanup_boundaries_preserves_snapshot_and_error(lifecycle, monkeypatch, original_error):
    import signal
    m, fake, initial = lifecycle
    restore = m.restore_services
    operation = fake.systemctl
    failure = m.SetupError('original restart failure')
    before = (m.CONFIG / 'embedder.env').read_bytes()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    boundaries = []
    def interrupted(cores, dependents):
        assert not fake.active
        boundaries.append('entry')
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)
        result = restore(cores, dependents)
        boundaries.append('exit')
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)
        return result
    def restart(*args):
        if original_error and args == ('restart', 'bob-embedder.service') and m.selection()['model'] == '0.6b':
            raise failure
        return operation(*args)
    monkeypatch.setattr(m, 'restore_services', interrupted)
    monkeypatch.setattr(m, 'systemctl', restart)
    with pytest.raises(m.SetupError) as caught:
        m.run_job('select', '0.6b')
    assert fake.active == initial
    assert boundaries == ['entry', 'exit']
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
    detail = json.loads((m.STATE / 'setup.json').read_text())['detail']
    if original_error:
        assert caught.value is failure
        assert 'original restart failure' in detail
        assert (m.CONFIG / 'embedder.env').read_bytes() == before
    else:
        assert 'cancelled' in detail
        assert m.selection()['model'] == '0.6b'


def test_repeated_sigterm_during_operation_and_rollback_keeps_original_error(lifecycle, monkeypatch):
    import signal
    m, fake, initial = lifecycle
    operation = fake.systemctl
    write = m.atomic_write
    before = (m.CONFIG / 'embedder.env').read_bytes()
    failure = m.SetupError('original operation failure')
    rolling_back = False
    def restart(*args):
        nonlocal rolling_back
        if args == ('restart', 'bob-embedder.service') and m.selection()['model'] == '0.6b':
            rolling_back = True
            raise failure
        return operation(*args)
    def rollback(*args, **kwargs):
        if rolling_back and args[0] in (m.CONFIG / 'embedder.env', m.DROPIN):
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
        return write(*args, **kwargs)
    monkeypatch.setattr(m, 'systemctl', restart)
    monkeypatch.setattr(m, 'atomic_write', rollback)
    with pytest.raises(m.SetupError) as caught:
        m.run_job('select', '0.6b')
    assert caught.value is failure
    assert fake.active == initial
    assert (m.CONFIG / 'embedder.env').read_bytes() == before


def test_sigterm_after_stop_cancels_before_publication_and_restores_snapshot(lifecycle, monkeypatch):
    import signal
    m, fake, initial = lifecycle
    operation = fake.systemctl
    before = (m.CONFIG / 'embedder.env').read_bytes()
    def stop(*args):
        result = operation(*args)
        if args[0] == 'stop':
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
        return result
    monkeypatch.setattr(m, 'systemctl', stop)
    with pytest.raises(m.SetupError, match='cancelled'):
        m.run_job('select', '0.6b')
    assert fake.active == initial
    assert (m.CONFIG / 'embedder.env').read_bytes() == before
