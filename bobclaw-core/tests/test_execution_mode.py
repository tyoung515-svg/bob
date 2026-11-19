"""Both execution modes, with no provider, socket or system configuration I/O."""
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import lane_config
from core import worker
from core.worker import client
from core.worker.contract import WorkerUnavailable


@pytest.fixture
def marker(tmp_path, monkeypatch):
    path = tmp_path / 'execution-boundary'
    monkeypatch.setattr(worker, 'EXECUTION_BOUNDARY', path)
    monkeypatch.delenv('BOB_EXECUTION_BOUNDARY', raising=False)
    return path


def test_unmarked_host_defaults_to_legacy_without_loading_worker_config(marker, monkeypatch):
    monkeypatch.setattr(client, 'configuration', lambda: pytest.fail('legacy must not load worker configuration'))
    assert worker.mode() == 'legacy'
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')
    assert worker.mode() == 'worker'
    monkeypatch.delenv('BOB_EXECUTION_BOUNDARY')
    assert worker.mode() == 'legacy'


@pytest.mark.parametrize('override', ['', 'legacy', 'off', '0', 'worker'])
def test_marker_cannot_be_disabled_by_environment(marker, monkeypatch, override):
    marker.write_text('worker\n')
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', override)
    assert worker.mode() == 'worker'
    monkeypatch.delenv('BOB_EXECUTION_BOUNDARY')
    assert worker.mode() == 'worker'


def test_marker_content_is_not_an_off_switch(marker):
    marker.write_text('legacy\n')
    assert worker.mode() == 'worker'


def test_inaccessible_marker_fails_closed(marker, monkeypatch):
    def denied(path):
        raise PermissionError('cannot inspect marker')
    monkeypatch.setattr(Path, 'lstat', denied)
    assert worker.mode() == 'worker'


@pytest.mark.parametrize('route', sorted(client.EXCLUDED))
def test_backend_admission_uses_mode_even_with_roster(marker, monkeypatch, route):
    monkeypatch.setenv('BOB_ROSTER_FILE', 'synthetic-roster')
    lane_config.require_backend(route)
    marker.touch()
    with pytest.raises(lane_config.LaneConfigurationError, match='unavailable'):
        lane_config.require_backend(route)


def test_marker_and_missing_lane_never_fall_back_to_local_git(marker, monkeypatch, tmp_path):
    from core.ledger import gitdag
    marker.touch()
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'legacy')
    monkeypatch.setattr(lane_config, 'get_lane', lambda: SimpleNamespace(values={}))
    monkeypatch.setattr(gitdag.subprocess, 'run', lambda *a, **kw: pytest.fail('local execution'))
    with pytest.raises(WorkerUnavailable, match='unavailable: lane has no worker boundary'):
        gitdag._git(tmp_path, 'status')


def test_worker_service_and_policy_failures_have_no_subprocess_fallback(marker, monkeypatch, tmp_path):
    from core.ledger import gitdag
    marker.touch()
    monkeypatch.setattr(gitdag.subprocess, 'run', lambda *a, **kw: pytest.fail('local execution'))
    monkeypatch.setattr(client, 'configuration', lambda: {'workspaces': {'scratch': str(tmp_path)}})
    monkeypatch.setattr(client, 'unavailable_reason', lambda profile: 'unavailable: worker service or network policy missing')
    with pytest.raises(WorkerUnavailable, match='service or network policy missing'):
        gitdag._git(tmp_path, 'status')


@pytest.mark.parametrize('failure', [FileNotFoundError(), PermissionError(), ValueError('invalid JSON')])
def test_missing_or_invalid_worker_config_is_explicitly_unavailable(marker, monkeypatch, failure):
    from core.worker import contract
    marker.touch()
    monkeypatch.setattr(lane_config, 'get_lane', lambda: SimpleNamespace(values={'BOB_WORKER_ID': 'test'}))
    def root_json(path):
        raise failure
    monkeypatch.setattr(contract, 'root_json', root_json)
    with pytest.raises(WorkerUnavailable, match='unavailable: worker configuration missing or invalid'):
        client.configuration()


@pytest.mark.parametrize('module,cls', [('claude_code', 'ClaudeCodeClient'),
    ('agy_code', 'AntigravityClient'), ('kimi_cli', 'KimiCliClient'), ('pi_code', 'PiCodeClient')])
@pytest.mark.asyncio
async def test_cli_health_checks_only_spawn_in_legacy(marker, monkeypatch, module, cls):
    backend = importlib.import_module('core.backends.' + module)
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'cli version', b'')))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(backend.asyncio, 'create_subprocess_exec', spawn)
    cli = getattr(backend, cls)()
    assert await cli.health_check()
    spawn.assert_awaited_once()
    marker.touch()
    with pytest.raises(WorkerUnavailable, match='unavailable'):
        await cli.health_check()
    spawn.assert_awaited_once()


def test_worker_docker_mode_refuses_even_if_ready_is_forged(marker, monkeypatch):
    from core.build import sandbox
    marker.touch()
    monkeypatch.setattr(sandbox._config, 'BUILD_SANDBOX', 'docker')
    monkeypatch.setattr(sandbox, 'docker_ready', lambda: True)
    with pytest.raises(sandbox.SandboxUnavailable, match='unavailable'):
        sandbox.resolve_mode()
