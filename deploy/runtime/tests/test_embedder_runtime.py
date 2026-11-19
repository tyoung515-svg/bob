"""PA2 offline contracts; real packaged ELF/systemd/WSL checks are acceptance probes."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def health(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'common', SimpleNamespace(read_env=lambda p: {}, require_root=lambda: None))
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(DATA=tmp_path, lanes=lambda: [], units=lambda name: []))
    monkeypatch.setitem(sys.modules, 'embedder', SimpleNamespace(selection=lambda: dict(
        model='4b', dimension=2560, path=str(tmp_path / 'models/Qwen3-Embedding-4B-Q4_K_M.gguf')),
        loaded_alias=lambda model: True))
    return load('pa2_health', RUNTIME / 'lib/health.py')


def unit(active='inactive', **kwargs):
    return dict(name='bob-embedder.service', ActiveState=active, query_ok=True, **kwargs)


def test_missing_model_reports_setup_without_contacting_server(health, monkeypatch):
    monkeypatch.setattr(health, 'endpoint', lambda port: pytest.fail('missing model must not probe server'))
    status = health.embedder_status(unit())
    assert status == dict(selected_model='4b', embedding_dimension=2560,
                          model_path=str(health.DATA / 'models/Qwen3-Embedding-4B-Q4_K_M.gguf'),
                          ready=False, setup_needed=True, status='setup-needed',
                          detail=f'embedder model not installed at {health.DATA / "models/Qwen3-Embedding-4B-Q4_K_M.gguf"}')
    # A failure must remain visible even if the model is also absent.
    assert health.embedder_status(unit('failed'))['status'] == 'failed'
    assert health.embedder_status({**unit(), 'query_ok': False})['status'] == 'failed'


@pytest.mark.parametrize('active,http,expected', [
    ('active', {'healthy': True, 'body': {'status': 'ok'}}, 'ready'),
    ('active', {'healthy': True, 'body': {'status': 'loading model'}}, 'starting'),
    ('active', {'healthy': True, 'body': []}, 'starting'),
    ('active', {'healthy': False}, 'starting'),
    ('activating', {'healthy': False}, 'starting'),
    ('inactive', {'healthy': True, 'body': {'status': 'ok'}}, 'failed'),
])
def test_model_present_requires_running_unit_and_real_readiness(health, monkeypatch, active, http, expected):
    model = health.DATA / 'models/Qwen3-Embedding-4B-Q4_K_M.gguf'
    model.parent.mkdir()
    model.touch()
    monkeypatch.setattr(health, 'endpoint', lambda port: http)
    status = health.embedder_status(unit(active))
    assert status['status'] == expected
    assert status['ready'] == (expected == 'ready')
    assert status['setup_needed'] is False


@pytest.mark.parametrize('failed', [False, True])
def test_health_receipt_reports_but_does_not_gate_on_unrelated_failed_units(health, monkeypatch, capsys, failed):
    (health.DATA / '.firstboot-complete').touch()
    monkeypatch.setattr(health, 'unit_status', lambda name: {**unit('inactive' if name == 'bob-embedder.service' else 'active'), 'name': name})
    rows = [dict(unit='kmod-static-nodes.service', active='failed')] if failed else []
    monkeypatch.setattr(health.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(rows)))
    # An unrelated failed system unit is reported but must not block setup.
    assert health.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['healthy'] is True
    assert receipt['lanes'] == {}
    assert isinstance(receipt['units'], list) and receipt['system_units'] == rows
    assert receipt['failed_units'] == (['kmod-static-nodes.service'] if failed else [])
    assert receipt['embedder']['status'] == 'setup-needed'


GGUF_FAIL = "E gguf_init_from_file: failed to open GGUF file '/nonexistent/bob-embedder-backend-probe.gguf' (No such file or directory)"


@pytest.mark.parametrize('output,code,passed', [
    # Real b11747 output shapes (captured from the built image's binaries).
    ('Available devices:\n  (none)', 0, False),
    ('I load_backend: loaded CPU backend from /opt/llama/libggml-cpu-x64.so\n' + GGUF_FAIL, 1, True),
    ('I load_backend: loaded CPU backend from /opt/llama/libggml-cpu-zen4.so\n' + GGUF_FAIL, 1, True),
    ('I load_backend: loaded CPU backend from /other/libggml-cpu-x64.so\n' + GGUF_FAIL, 1, False),
    ('I load_backend: loaded CPU backend from /opt/llama/libggml-cpu-x64.so', 1, False),
    ('E load_backend: failed to load /opt/llama: Is a directory\nE llama_model_load_from_file_impl: no backends are loaded. hint', 1, False),
    ('E llama_model_load_from_file_impl: no backends are loaded. hint\n' + GGUF_FAIL, 1, False),
])
def test_build_check_requires_successful_packaged_cpu_load(tmp_path, output, code, passed):
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('bash unavailable for offline shell gate test')
    available = subprocess.run([bash, '-c', 'exit 0'], capture_output=True, text=True)
    if available.returncode:
        pytest.skip('host cannot launch bash: ' + available.stderr.strip())
    fake = tmp_path / 'fake-llama'
    canned = tmp_path / 'llama-output.txt'
    canned.write_text(output + '\n', newline='\n')
    fake.write_text('#!/bin/sh\n[ "$1" = --verbose ] || exit 9\ncat "' + canned.as_posix() + '" >&2\nexit ' + str(code) + '\n', newline='\n')
    checker = tmp_path / 'check.sh'
    # Substitute only the executable, retaining the real production shell gate.
    checker.write_text((RUNTIME / 'check-embedder-backend.sh').read_text().replace(
        '/usr/local/bin/llama-server', 'sh "' + fake.as_posix() + '"'), newline='\n')
    result = subprocess.run([bash, str(checker)], capture_output=True, text=True)
    assert (result.returncode == 0) is passed, result.stderr


@pytest.fixture
def acceptance():
    return load('pa2_embedder_acceptance', RUNTIME / 'tests/embedder_acceptance.py')


@pytest.mark.parametrize('kind', ['good', 'zero', 'nan', 'infinite', 'wrong-dim', 'one', 'same'])
def test_vector_probe_checks_count_dimension_finiteness_nonzero_and_distinction(acceptance, kind):
    vectors = [[1.0] + [0.0] * 2559, [0.0, 1.0] + [0.0] * 2558]
    if kind == 'zero': vectors[0] = [0.0] * 2560
    if kind == 'nan': vectors[0][0] = float('nan')
    if kind == 'infinite': vectors[0][0] = float('inf')
    if kind == 'wrong-dim': vectors[0].pop()
    if kind == 'one': vectors.pop()
    if kind == 'same': vectors[1] = vectors[0][:]
    assert acceptance.vector_receipt(vectors)['valid'] is (kind == 'good')


@pytest.mark.parametrize('failed', [False, True])
def test_acceptance_observes_boot_without_repair_and_skips_unsupplied_model(acceptance, tmp_path, monkeypatch, capsys, failed):
    monkeypatch.setattr(acceptance, 'MODEL', tmp_path / 'absent.gguf')
    monkeypatch.delenv('BOB_ACCEPT_EMBED_MODEL', raising=False)
    monkeypatch.setattr(acceptance.os, 'access', lambda *a: True)
    calls, rows = [], []
    status = dict(status='setup-needed', setup_needed=True, ready=False,
                  detail=f'embedder model not installed at {acceptance.MODEL}')
    def command(*args, **kwargs):
        calls.append(args)
        out = ''
        if args[:2] == ('systemctl', 'is-system-running'): out = 'degraded' if failed else 'running'
        if args[:2] == ('systemctl', '--failed'): out = 'kmod-static-nodes.service loaded failed failed' if failed else ''
        if args[0] == 'bob-health': out = json.dumps(dict(healthy=not failed, lanes={}, embedder=status))
        return SimpleNamespace(returncode=0, stdout=out, stderr='')
    monkeypatch.setattr(acceptance, 'command', command)
    assert acceptance.run('test', lambda probe, passed, **kw: rows.append((probe, passed))) == int(failed)
    assert calls[0][:2] == ('systemctl', 'is-system-running')
    assert calls[1][:2] == ('systemctl', '--failed')
    assert not any('restart' in call or 'reset-failed' in call for call in calls)
    assert ('embedder.missing-model', True) in rows
    skipped = json.loads(capsys.readouterr().out)
    assert skipped['probe'] == 'embedder.lane-client' and skipped['passed'] is None
