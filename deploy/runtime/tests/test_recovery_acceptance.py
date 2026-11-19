"""Acceptance harness failures retain actionable exception diagnostics."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('failure',[RuntimeError('receipt_timeout'),ValueError('diagnostic detail')])
def test_main_records_exception_type_and_message(monkeypatch,capsys,failure):
    # Import without installed-only dependencies or an installed lane. No process
    # or mutation runs; exercise the actual main/error/JSONL reporting path.
    with monkeypatch.context() as imports:
        imports.setattr(sys,'path',list(sys.path))
        imports.setitem(sys.modules,'password_acceptance',SimpleNamespace(**{
            name:None for name in ('command','http','pids','next_code','wait_gateway','broker','FAMILY')}))
        imports.setitem(sys.modules,'lane',SimpleNamespace(validate_name=lambda name:name))
        spec=importlib.util.spec_from_file_location('recovery_acceptance',Path(__file__).with_name('recovery_acceptance.py'))
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        async def fail(names,record,cleanups):
            raise failure
        imports.setattr(module,'run',fail)
        imports.setattr(module.os,'geteuid',lambda:0,raising=False)
        imports.setattr(sys,'argv',['recovery_acceptance.py','--lane-a','test-a','--lane-b','test-b'])
        assert module.main()==1
    probes=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert probes==[
        dict(probe='s3g.harness-'+type(failure).__name__,passed=False,
             exception_type=type(failure).__name__,exception_message=str(failure)),
        dict(probe='s3g.overall',passed=False),
    ]
