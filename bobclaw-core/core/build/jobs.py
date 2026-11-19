"""Async worker-mode build gates; legacy sandbox behavior stays in its callers."""
import asyncio
import subprocess

from core.worker.client import execute_async
from .skeleton import parse_pytest_summary


async def scaffold(workspace, units):
    from core.execution.models import canonical
    from core.execution.workspaces import snapshot
    frozen = await asyncio.to_thread(snapshot, workspace)
    receipt = await execute_async('checked', cwd=workspace,
        input=canonical({'operation':'python.scaffold','input_digest':frozen['digest'],'build_contracts':units}),timeout=120)
    if receipt.get('outcome') != 'succeeded' or receipt.get('returncode') != 0:
        raise RuntimeError('worker scaffold failed; inspect job receipt')


async def collect(workspace):
    import re
    builds, _ = await gate(workspace, 'python.import')
    collected, out = await gate(workspace, 'python.collect')
    match = re.search(r'(\d+) test', out)
    return builds, int(match[1]) if collected and match else 0


async def gate(workspace, operation, *, timeout=900):
    from core.tool_jobs.checked_runner import check_command
    args = check_command(operation)[1:]
    receipt = await execute_async('python', cwd=workspace, args=args, timeout=timeout)
    if receipt.get('killed') or receipt.get('outcome') == 'timed_out':
        raise subprocess.TimeoutExpired(operation, timeout)
    out = receipt['stdout'].decode('utf-8', 'replace') + receipt['stderr'].decode('utf-8', 'replace')
    return receipt['returncode'] == 0, out


async def verify(workspace, *, timeout=900):
    builds, _ = await gate(workspace, 'python.import', timeout=timeout)
    collected, _ = await gate(workspace, 'python.collect', timeout=timeout)
    passed, output = await gate(workspace, 'python.pytest', timeout=timeout)
    runs, _ = await gate(workspace, 'python.cli', timeout=timeout)
    report = parse_pytest_summary(output)
    if not passed and not report['failed'] and not report['errors']:
        report['errors'] = 1
    return {'builds': builds and collected, **report, 'runs': runs}
