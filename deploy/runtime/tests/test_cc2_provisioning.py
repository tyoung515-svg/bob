"""Root provisioning of the independent Claude supervisor; no model requests."""
import importlib.util
import json
import os
from pathlib import Path
import pytest
from test_provisioning import runtime, ROOT, assert_permissions

pytestmark = pytest.mark.skipif(os.name == 'nt' or getattr(os, 'geteuid', lambda: -1)() != 0,
                               reason='root Linux provisioning syscalls')


def test_fresh_and_idempotent_upgrade_preserve_other_profiles(runtime):
    r = runtime
    r.lane.add('fresh', 'test-password')
    root = r.lane.FAMILY / 'fresh/bob-memory'
    ids = r.worker.load_identity('fresh')
    cfg_path = r.worker.CONFIG / (ids['lane_id']+'.json')
    before = json.loads(cfg_path.read_text())
    key = ids['lane_id']
    assert before['claude_turn_socket'] == f'/run/bob-workers/{key}/claude-turn.sock'
    service = r.systemd / f'bob-claude-turn@{key}.service.d/identity.conf'
    text = service.read_text()
    assert f'BindPaths={root}/cli/claude' in text
    assert '/projects' not in text and '/scratch' not in text and 'codex-home' not in text
    socket = r.systemd / f'bob-claude-turn@{key}.socket.d/identity.conf'
    assert socket.read_text() == f'[Socket]\nSocketUser={ids["service_user"]}\n'
    spec = importlib.util.spec_from_file_location('cc2_hooks', ROOT/'deploy/runtime/lib/upgrade_hooks.py')
    hooks = importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
    for _ in range(2):
        assert hooks.claude_turns('fresh') == [f'bob-claude-turn@{key}.socket']
        assert json.loads(cfg_path.read_text())['profiles'] == before['profiles']
    r.worker.set_admission(ids, False)
    assert json.loads(cfg_path.read_text())['claude_turn_socket'].endswith('migration-disabled-claude')
    hooks.claude_turns('fresh')
    assert json.loads(cfg_path.read_text())['claude_turn_socket'].endswith('migration-disabled-claude')
    r.worker.set_admission(ids, True)
    assert json.loads(cfg_path.read_text())['claude_turn_socket'].endswith('claude-turn.sock')


def test_turn_unit_keeps_worker_hardening_and_own_cgroup():
    text = (ROOT/'deploy/runtime/systemd/bob-claude-turn@.service').read_text()
    for required in ('NoNewPrivileges=yes', 'PrivateTmp=yes', 'ProtectProc=invisible',
                     'ProtectSystem=strict', 'KillMode=control-group', 'RestrictNamespaces=yes'):
        assert required in text
    assert '-I -m core.worker.claude_turn' in text
    assert 'bob-worker-journal@' not in text
