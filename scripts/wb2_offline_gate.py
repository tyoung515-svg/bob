"""WB2 local fake-model gate. No provider or production worker is contacted."""
import os
from pathlib import Path
import sys
import uuid
import types
import importlib.util
import pytest

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'bobclaw-core'))
deps=root/'bobclaw-core/test-db/wb2-runtime-deps'
if deps.is_dir(): sys.path.insert(0,str(deps))
if importlib.util.find_spec('langchain_mcp_adapters') is None:
    package=types.ModuleType('langchain_mcp_adapters'); package.__path__=[]
    client=types.ModuleType('langchain_mcp_adapters.client')
    class UnavailableMCP:
        def __init__(self,*args,**kwargs): raise RuntimeError('MCP transport is outside this offline gate.')
    client.MultiServerMCPClient=UnavailableMCP
    sys.modules['langchain_mcp_adapters']=package
    sys.modules['langchain_mcp_adapters.client']=client
os.chdir(root/'bobclaw-core')
os.environ.setdefault('BOB_ROSTER_FILE',str(root/'bobclaw-core/tests/fixtures/deployment/roster.yaml'))
os.environ.setdefault('KIMI_MODEL','kimi-k2.7-code')
if '--write-fixtures' in sys.argv: os.environ['WORKFLOW_WRITE_FIXTURES']='1'

class WorkspaceTemp:
    def pytest_collection_modifyitems(self,items):
        if os.name=='nt':
            for item in items:
                if item.name=='test_connect_tunnel_client_reset_after_traffic_is_a_normal_close[False-error]':
                    item.add_marker(pytest.mark.skip(reason='Windows Proactor reports a zero-byte reset as EOF; POSIX reset semantics require the Linux gate.'))
    @pytest.fixture
    def tmp_path(self):
        path=root/'bobclaw-core/test-db'/('wb2-'+uuid.uuid4().hex)
        path.mkdir(parents=True,mode=0o777)
        return path

args=['--noconftest','-q','-p','no:cacheprovider','--asyncio-mode=auto',
      'tests/test_workflow_controller.py','tests/test_tool_loop.py',
      'tests/test_builder_session.py','tests/test_builder_workspace.py','tests/test_builder_network.py',
      '--tb=short']
if '--controller-only' in sys.argv: args=[*args[:5],'tests/test_workflow_controller.py','--tb=short']
if '--execution-regressions' in sys.argv:
    args=[*args[:5],'tests/test_c6_task_vouch.py','tests/test_job_time_ladder.py',
          'tests/test_checked_edits.py','tests/test_day_one_publication.py','tests/test_execution_mode.py','--tb=short']
if '--fail-fast' in sys.argv: args.append('-x')
raise SystemExit(pytest.main(args,plugins=[WorkspaceTemp()]))
