"""Repeatable offline WB2B tests with workspace-local temp files and GL18 deps."""
import os
from pathlib import Path
import sys
import types
import uuid
import pytest

root = Path(__file__).resolve().parents[1]
deps = root.parent / 'runs/golive/gl18-deps'
sys.path[:0] = [str(deps), str(root / 'bobclaw-core'), str(root.parent / 'runs/golive')]
os.environ.setdefault('BOB_ROSTER_FILE', str(root / 'bobclaw-core/tests/fixtures/deployment/roster.yaml'))
os.environ.setdefault('KIMI_MODEL', 'kimi-k2.7-code')
os.environ.setdefault('DEEPSEEK_API_KEY', 'synthetic-test-only')
os.environ.setdefault('JWT_SECRET', 'wb2b-synthetic-test-signing-key-only-123456789')
os.environ.setdefault('BOBCLAW_PASSWORD', 'wb2b-synthetic-owner-password')
os.environ.setdefault('TOTP_SECRET', 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')
gateway = '--gateway' in sys.argv
if '--write-fixtures' in sys.argv:
    os.environ['WB2B_WRITE_FIXTURES'] = '1'
if gateway:
    os.chdir(root / 'bobclaw-gateway')
    sys.path.insert(0, str(root / 'bobclaw-gateway'))
    if os.name == 'nt':
        shim = types.ModuleType('fcntl')
        shim.LOCK_EX, shim.LOCK_SH, shim.LOCK_UN, shim.LOCK_NB = 2, 1, 8, 4
        def unavailable(*args, **kwargs):
            raise AssertionError('POSIX locking is outside this Windows HTTP gate')
        shim.flock = unavailable
        sys.modules['fcntl'] = shim
    files = ['tests/test_workflow_observation.py', 'tests/test_workflows.py',
        'tests/test_execution_proxy.py', 'tests/test_chat_concurrency.py',
        'tests/test_bob_guide.py', 'tests/test_day_one_routes.py',
        'tests/test_gl9_turn_budget.py', 'tests/test_gl9_closing_chat.py',
        'tests/test_c7_pc_transport.py', 'tests/test_rate_limit.py', 'tests/test_broker_rate_limit.py']
else:
    os.chdir(root / 'bobclaw-core')
    files = ['tests/test_workflow_tools.py', 'tests/test_workflow_schema.py',
        'tests/test_workflow_authority.py', 'tests/test_workflow_postgres.py',
        'tests/test_durable_execution.py', 'tests/test_execution_migration_order.py', 'tests/test_c7_pc.py',
        'tests/test_bob_guide.py', 'tests/test_bob_guide_mcp.py',
        'tests/test_face_prompt_routing_profile.py', 'tests/test_builder_session.py',
        'tests/test_gl7_assistant.py', 'tests/test_gl11_managed_handoff.py',
        'tests/test_mcp_server.py', 'tests/test_mcp_action_tools.py',
        'tests/test_agent_memory_mcp.py', 'tests/test_agent_services_mcp.py',
        'tests/test_tool_loop.py', 'tests/test_day_one_chat.py']


class LocalTests:
    @pytest.fixture(autouse=True)
    def gateway_storage(self, monkeypatch, request):
        if gateway:
            tmp_path = request.getfixturevalue('tmp_path')
            monkeypatch.setenv('GATEWAY_SQLITE_PATH', str(tmp_path / 'gateway.db'))

    @pytest.fixture
    def tmp_path(self):
        path = root / 'bobclaw-core/test-db' / ('wb2b-' + uuid.uuid4().hex)
        path.mkdir(parents=True)
        return path

    def pytest_sessionstart(self, session):
        if gateway:
            from routers import agent_services
            setup = agent_services.setup
            destination = root / 'bobclaw-gateway/test-db' / ('wb2b-' + uuid.uuid4().hex)
            (destination / 'runs').mkdir(parents=True)
            agent_services.setup = lambda app: setup(app, root=destination)


extra = [a for a in sys.argv[1:] if a not in {'--gateway', '--write-fixtures'}]
raise SystemExit(pytest.main(['--noconftest', '-q', '-p', 'no:cacheprovider',
    '--asyncio-mode=auto', '--tb=short', *files, *extra], plugins=[LocalTests()]))
