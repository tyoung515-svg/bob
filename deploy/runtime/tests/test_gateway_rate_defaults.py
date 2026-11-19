"""Desktop gateway environment defaults, with filesystem/ownership writes captured.

The real root Linux provisioning suite checks publication and file permissions.
"""
from contextlib import nullcontext
import importlib.util
from pathlib import Path, PurePosixPath
import shlex
import sys
from types import SimpleNamespace

import pytest

LIB = Path(__file__).resolve().parents[1] / 'lib'


@pytest.mark.parametrize('split', [False, True], ids=['legacy', 'split'])
@pytest.mark.parametrize('shared_limits,lane_limits,expected', [
    ({}, {}, ('600', '300')),
    ({'RATE_LIMIT_PER_MINUTE': '900', 'RATE_LIMIT_BURST': '450'}, {}, ('900', '450')),
    ({'RATE_LIMIT_PER_MINUTE': '900', 'RATE_LIMIT_BURST': '450'},
     {'RATE_LIMIT_PER_MINUTE': '120', 'RATE_LIMIT_BURST': '75'}, ('120', '75')),
    ({}, {'RATE_LIMIT_BURST': '80'}, ('600', '80')),
])
def test_generated_gateway_defaults_preserve_explicit_configuration(monkeypatch, split,
                                                                   shared_limits, lane_limits, expected):
    monkeypatch.syspath_prepend(str(LIB.parents[2] / 'bobclaw-core'))
    if sys.platform == 'win32':
        monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace())
    def load(name):
        spec = importlib.util.spec_from_file_location(name, LIB / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module
    common, worker = load('common'), load('worker')
    class EnvPath(PurePosixPath):
        def exists(self):
            return False
        def unlink(self):
            pass
    root = EnvPath('/family/owner/bob-memory')
    lane = {'JWT_SECRET': 'keep-jwt', 'BOBCLAW_PASSWORD': 'keep-password', 'TOTP_SECRET': 'keep-factor',
            'BOB_BRIDGE_ENGINE': 'http://127.0.0.1:7835', 'BOB_BRIDGE_PORT': '8902', **lane_limits}
    shared = {'POSTGRES_URL': 'keep-url', 'REDIS_URL': 'keep-redis', **shared_limits}
    original_lane, original_shared = dict(lane), dict(shared)
    monkeypatch.setattr(common, 'read_env', lambda p: dict(lane if p.name == 'lane.env' else shared))
    ids = {'service_gid': 12, 'lane_id': 'a' * 12}
    if split:
        ids.update(gateway_uid=13, gateway_gid=13, bridge_uid=14, bridge_gid=14)
    monkeypatch.setattr(worker, 'load_identity', lambda name: dict(ids))
    monkeypatch.setattr(worker, 'SERVICE_ENVS', EnvPath('/service-env'))
    monkeypatch.setattr(worker, 'GATEWAY_STATE', EnvPath('/gateway-state'))
    monkeypatch.setattr(worker, 'os', SimpleNamespace(
        chmod=lambda *a: None, chown=lambda *a: None, fchmod=lambda *a: None, fchown=lambda *a: None))
    from core.worker import auth_file
    monkeypatch.setattr(auth_file, 'directory', lambda *a, **kw: nullcontext(1))
    monkeypatch.setitem(sys.modules, 'core.lane_config', SimpleNamespace(
        read_lane=lambda *a, **kw: SimpleNamespace(backends={})))
    writes = {}
    def capture(path, text, *args):
        writes[str(path)] = dict(line.split('=', 1) for line in shlex.split(text))
    monkeypatch.setattr(worker, 'private_replace', capture)
    for _ in range(2):  # Fresh provisioning and an upgrade refresh use this same generator.
        worker.service_environments(root)
        gateway_path = '/service-env/' + ids['lane_id'] + '-gateway.env' if split else str(root / 'gateway.env')
        gateway = writes[gateway_path]
        assert (gateway['RATE_LIMIT_PER_MINUTE'], gateway['RATE_LIMIT_BURST']) == expected
        assert gateway['JWT_SECRET'] == lane['JWT_SECRET']
        assert gateway['BOBCLAW_PASSWORD'] == lane['BOBCLAW_PASSWORD']
        assert gateway['TOTP_SECRET'] == lane['TOTP_SECRET']
        assert shared == original_shared and lane == original_lane
