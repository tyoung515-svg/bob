"""Source seam checks and launcher behavior; not native app acceptance."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
class Port(unittest.TestCase):
    def test_registration(self):
        source = (ROOT / 'shell/src-tauri/src/lib.rs').read_text()
        for required in ['mod portal;', 'portal::PortalState', 'portal::PORTAL_COMMANDS', 'portal::dispatch', 'InvokeBody::Json', 'webview_ref().label()', 'WindowEvent::Destroyed', 'functional::app_boot', 'workbench::workbench_snapshot']:
            self.assertIn(required, source)
    def test_launcher_missing_binary(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/linux/launch-portal.sh')], env={**os.environ, 'BOB_PORTAL_BINARY': '/nonexistent/bob-portal'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('binary', result.stderr)
    def test_launcher_default_and_private_root(self):
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp)
            launcher = candidate / 'scripts/linux/launch-portal.sh'
            launcher.parent.mkdir(parents=True)
            launcher.write_text((ROOT / 'scripts/linux/launch-portal.sh').read_text())
            binary = candidate / 'shell/src-tauri/target/release/bob-slice-shell'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nprintf "%s\\n" "$BOB_PORTAL_DATA_ROOT" "$BOB_PORTAL_TARGET" "$BOB_PORTAL_BROKER" "$BOB_FUNCTIONAL_DATA_ROOT" "$BOB_PREVIEW_DATA_ROOT" "$BOB_PORTAL_SSH_CONFIG" "$@"\n')
            binary.chmod(0o700)
            env = {k:v for k,v in os.environ.items() if not k.startswith('BOB_PORTAL_') and k not in ('BOB_FUNCTIONAL_DATA_ROOT', 'BOB_PREVIEW_DATA_ROOT')}
            result = subprocess.run(['bash', str(launcher), '--smoke'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [str(candidate / '.portal-linux-data'), '', '', str(candidate / '.portal-linux-data/local-workbench'), str(candidate / '.portal-linux-data/slice-preview'), '', '--smoke'])
            config = candidate / '.portal-local/ssh.conf'
            config.parent.mkdir()
            config.write_text('Host bob-portal-server\n HostName 192.0.2.1\n')
            configured = subprocess.run(['bash', str(launcher)], env=env, capture_output=True, text=True)
            self.assertEqual(configured.returncode, 0, configured.stderr)
            self.assertEqual(configured.stdout.splitlines()[1], 'bob-portal-server')
            self.assertEqual(configured.stdout.splitlines()[5], str(config))
if __name__ == '__main__': unittest.main()
