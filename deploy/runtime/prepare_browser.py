"""Build-only, checked adaptation of the pinned MCP package to CDP pipes.

MCP 0.0.40 unconditionally injects a TCP cdpPort, even in isolated mode.
Its CLI/config cannot disable that injection. Removing it makes Playwright use
its default --remote-debugging-pipe transport. Reject upstream/layout drift.
"""
import json
from pathlib import Path
import sys


PLAYWRIGHT = '1.56.0-alpha-1758750661000'
SHELL_REVISION = '1191'
TCP_INJECTION = '''async function injectCdpPort(browserConfig) {
  if (browserConfig.browserName === "chromium")
    browserConfig.launchOptions.cdpPort = await findFreePort();
}'''
PIPE_ONLY = '''async function injectCdpPort(browserConfig) {
  // Bob worker: leave cdpPort unset so Playwright uses its inherited CDP pipe.
}'''


def prepare(modules: Path):
    mcp = modules / '@playwright/mcp'
    packages = {name: json.loads((modules / name / 'package.json').read_text())
                for name in ('@playwright/mcp', 'playwright', 'playwright-core')}
    if packages['@playwright/mcp']['version'] != '0.0.40':
        raise ValueError('unexpected MCP version')
    for name in ('playwright', 'playwright-core'):
        if (packages[name]['version'] != PLAYWRIGHT or
                packages['@playwright/mcp']['dependencies'][name] != PLAYWRIGHT):
            raise ValueError('unexpected Playwright version')
    # All three exact pins must use the same hoisted engine/browser manifest.
    if any((parent / 'node_modules' / name).exists()
           for parent in (mcp, modules / 'playwright')
           for name in ('playwright', 'playwright-core')):
        raise ValueError('unexpected nested Playwright dependency')
    browsers = json.loads((modules / 'playwright-core/browsers.json').read_text())['browsers']
    shells = [row for row in browsers if row['name'] == 'chromium-headless-shell']
    if len(shells) != 1 or shells[0]['revision'] != SHELL_REVISION:
        raise ValueError('unexpected headless shell revision')
    factory = modules / 'playwright/lib/mcp/browser/browserContextFactory.js'
    source = factory.read_text(encoding='utf-8')
    if source.count(TCP_INJECTION) != 1 or source.count('await injectCdpPort(this.config.browser);') != 2:
        raise ValueError('unexpected MCP CDP injection source')
    factory.write_text(source.replace(TCP_INJECTION, PIPE_ONLY), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    prepare(Path(sys.argv[1]))
