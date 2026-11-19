"""Windows-only isolated gateway/GL16 regression gate, no POSIX lock execution."""
import os
from pathlib import Path
import sys
import types
import uuid
import pytest

root=Path(__file__).resolve().parents[1]
os.chdir(root/'bobclaw-gateway')
sys.path.insert(0,str(root/'bobclaw-gateway'))
sys.path.insert(0,str(root/'bobclaw-core'))
if os.name=='nt':
    shim=types.ModuleType('fcntl')
    shim.LOCK_EX,shim.LOCK_SH,shim.LOCK_UN,shim.LOCK_NB=2,1,8,4
    def unavailable(*args,**kwargs):
        raise AssertionError('POSIX locking must not run in this HTTP/broker gate')
    shim.flock=unavailable
    sys.modules['fcntl']=shim


class LocalServices:
    def pytest_sessionstart(self,session):
        from routers import agent_services
        setup=agent_services.setup
        destination=root/'bobclaw-gateway/test-db'/('wb1-services-'+uuid.uuid4().hex)
        (destination/'runs').mkdir(parents=True)
        agent_services.setup=lambda app:setup(app,root=destination)


raise SystemExit(pytest.main(['-q','-p','no:cacheprovider',
    'tests/test_workflows.py','tests/test_execution_proxy.py','tests/test_chat_concurrency.py',
    'tests/test_gl9_turn_budget.py','tests/test_gl9_closing_chat.py','tests/test_c7_pc_transport.py',
    '--tb=short'],plugins=[LocalServices()]))
