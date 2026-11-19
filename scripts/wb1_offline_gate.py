"""Local WB1 regression gate without the unrelated model-loop Redis fixture."""
import os
from pathlib import Path
import sys
import pytest
import uuid

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'bobclaw-core'))
os.chdir(root/'bobclaw-core')
os.environ.setdefault('BOB_ROSTER_FILE',str(root/'bobclaw-core/tests/fixtures/deployment/roster.yaml'))
os.environ.setdefault('KIMI_MODEL','kimi-k2.7-code')
if '--write-fixtures' in sys.argv:
    os.environ['WORKFLOW_WRITE_FIXTURES']='1'
args=['--noconftest','-q','-p','no:cacheprovider','--asyncio-mode=auto',
 'tests/test_workflow_schema.py','tests/test_workflow_authority.py','tests/test_workflow_postgres.py',
 'tests/test_durable_execution.py','tests/test_execution_migration_order.py',
 'tests/test_builder_session.py','tests/test_c7_pc.py',
 '--tb=short']
class WorkspaceTemp:
    @pytest.fixture
    def tmp_path(self):
        path=root/'bobclaw-core/test-db'/('wb1-'+uuid.uuid4().hex)
        path.mkdir(parents=True)
        return path

raise SystemExit(pytest.main(args,plugins=[WorkspaceTemp()]))
