"""Offline browser selection and pinned pipe-only build adaptation regressions."""
from contextlib import asynccontextmanager
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.worker import mcp_runner

ROOT = Path(__file__).resolve().parents[2]
SHELL = '/opt/bob-browsers/chromium_headless_shell-1191/chrome-linux/headless_shell'


@pytest.mark.parametrize('operation', ['list', 'call', 'sequence'])
async def test_browser_requests_use_only_installed_headless_shell(monkeypatch, operation, capsys, tmp_path):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    calls, launches = [], []
    @asynccontextmanager
    async def transport(args):
        launches.append(args)
        yield None, None
    class Session:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass
        async def list_tools(self): return SimpleNamespace(tools=[])
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(model_dump=lambda **kwargs: {'isError': False})
    monkeypatch.setattr(mcp_runner, 'transport', transport)
    monkeypatch.setattr(mcp_runner, 'ClientSession', Session)
    def access(path, mode):
        assert path == SHELL and mode == mcp_runner.os.X_OK
        return True
    monkeypatch.setattr(mcp_runner.os, 'access', access)
    sequence = [{'name': 'browser_navigate', 'arguments': {'url': 'data:text/html,<h1>ok</h1>'}},
                {'name': 'browser_snapshot', 'arguments': {}}]
    request = dict(server='browser', operation=operation, **sequence[0], calls=sequence)
    monkeypatch.setattr(mcp_runner.sys, 'stdin', io.StringIO(json.dumps(request)))
    await mcp_runner.main()
    assert len(launches) == 1 and launches[0][:6] == [
        '/opt/bob-mcp/node_modules/@playwright/mcp/cli.js', '--headless',
        '--isolated', '--no-sandbox', '--executable-path', SHELL]
    assert launches[0][6] == '--config'
    assert json.loads(Path(launches[0][7]).read_text()) == {
        'browser': {'launchOptions': {'args': ['--no-zygote']}}}
    assert calls == ([] if operation == 'list' else
                     [(c['name'], c['arguments']) for c in sequence[:1 if operation == 'call' else 2]])
    assert json.loads(capsys.readouterr().out) is not None


async def test_missing_headless_shell_fails_without_browser_fallback(monkeypatch):
    monkeypatch.setattr(mcp_runner.os, 'access', lambda *args: False)
    monkeypatch.setattr(mcp_runner.sys, 'stdin', io.StringIO('{"server":"browser","operation":"list"}'))
    monkeypatch.setattr(mcp_runner, 'transport', lambda *args: pytest.fail('must not launch another browser'))
    with pytest.raises(ValueError, match='headless shell unavailable'):
        await mcp_runner.main()


@pytest.fixture
def browser_build(tmp_path):
    spec = importlib.util.spec_from_file_location('prepare_browser', ROOT / 'deploy/runtime/prepare_browser.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    packages = {'@playwright/mcp': {'version': '0.0.40', 'dependencies': {
                    'playwright': module.PLAYWRIGHT, 'playwright-core': module.PLAYWRIGHT}},
                'playwright': {'version': module.PLAYWRIGHT},
                'playwright-core': {'version': module.PLAYWRIGHT}}
    for name, package in packages.items():
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / 'package.json').write_text(json.dumps(package))
    (tmp_path / 'playwright-core/browsers.json').write_text(json.dumps({'browsers': [
        {'name': 'chromium-headless-shell', 'revision': module.SHELL_REVISION}]}))
    factory = tmp_path / 'playwright/lib/mcp/browser/browserContextFactory.js'
    factory.parent.mkdir(parents=True)
    factory.write_text('await injectCdpPort(this.config.browser);\n' * 2 + module.TCP_INJECTION + '\n')
    return module, tmp_path, factory


def test_build_removes_forced_tcp_cdp_injection(browser_build):
    module, modules, factory = browser_build
    before = factory.read_text()
    module.prepare(modules)
    after = factory.read_text()
    assert after == before.replace(module.TCP_INJECTION, module.PIPE_ONLY)
    assert 'launchOptions.cdpPort =' not in after
    assert '--remote-debugging-port' not in after
    # Applying twice is rejected, rather than silently accepting unknown input.
    with pytest.raises(ValueError, match='injection source'):
        module.prepare(modules)


@pytest.mark.parametrize('drift', ['mcp', 'playwright', 'playwright-core', 'dependency',
                                  'nested-mcp', 'nested-engine', 'revision', 'source', 'call-sites'])
def test_build_rejects_dependency_browser_and_source_drift(browser_build, drift):
    module, modules, factory = browser_build
    before = factory.read_bytes()
    if drift in {'mcp', 'playwright', 'playwright-core', 'dependency'}:
        package = modules / ('@playwright/mcp' if drift in {'mcp', 'dependency'} else drift) / 'package.json'
        value = json.loads(package.read_text())
        if drift == 'dependency':
            value['dependencies']['playwright'] = 'wrong'
        else:
            value['version'] = 'wrong'
        package.write_text(json.dumps(value))
    elif drift.startswith('nested-'):
        parent = '@playwright/mcp' if drift == 'nested-mcp' else 'playwright'
        (modules / parent / 'node_modules/playwright-core').mkdir(parents=True)
    elif drift == 'revision':
        (modules / 'playwright-core/browsers.json').write_text('{"browsers":[]}')
    else:
        factory.write_text(factory.read_text().replace(
            'await findFreePort()' if drift == 'source' else 'await injectCdpPort(this.config.browser);', 'changed'))
        before = factory.read_bytes()
    with pytest.raises(ValueError, match='unexpected'):
        module.prepare(modules)
    assert factory.read_bytes() == before


def test_image_installs_mcp_matching_shell_and_checked_pipe_adaptation():
    image = (ROOT / 'deploy/runtime/Containerfile').read_text()
    assert 'playwright@1.56.0-alpha-1758750661000 playwright-core@1.56.0-alpha-1758750661000' in image
    assert 'prepare_browser.py /opt/bob-mcp/node_modules' in image
    assert 'playwright install --with-deps --only-shell chromium' in image
    assert 'test -x ' + SHELL in image
