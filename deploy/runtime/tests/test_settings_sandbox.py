"""Exercise the shipped settings mount restrictions with a real systemd unit.

Root Linux/systemd only. No installed lanes, credentials or Bob units are used.
"""
from configparser import ConfigParser
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import pytest


pytestmark=pytest.mark.skipif(
    os.name!='posix' or getattr(os,'geteuid',lambda:-1)()!=0
    or not Path('/run/systemd/system').is_dir() or not shutil.which('systemd-run'),
    reason='requires root Linux with a running systemd manager')


@pytest.mark.parametrize('kind',['control','receipt'])
def test_installed_sandbox_allows_only_control_to_publish_portal(kind):
    unit=Path(__file__).parents[1]/'systemd'/f'bob-settings-{kind}@.service'
    # Preserve repeated ReadWritePaths directives, which systemd accumulates.
    parser=ConfigParser(strict=False,interpolation=None)
    parser.read(unit)
    service=parser['Service']
    writable=[line.partition('=')[2].strip() for line in unit.read_text().splitlines()
              if line.startswith('ReadWritePaths=')]
    assert service['ProtectSystem']=='strict'
    assert service['NoNewPrivileges']=='yes'
    assert service['ProtectHome']=='yes'
    with tempfile.TemporaryDirectory(prefix='bob-settings-sandbox-',dir='/run') as temporary:
        base=Path(temporary)
        settings,portal,embedder,outside=[base/name for name in ('settings','portal','embedder','outside')]
        for directory in (settings,portal,embedder,outside):
            directory.mkdir()
        destinations={'/var/lib/bob-settings':settings,'/var/lib/bob-portal':portal,
                      '/var/lib/bob-embedder':embedder}
        argv=['systemd-run','--quiet','--wait','--pipe','--collect',
              '--unit=bob-settings-sandbox-'+uuid.uuid4().hex]
        for key in ('ProtectSystem','NoNewPrivileges','ProtectHome','PrivateTmp','RestrictAddressFamilies'):
            argv.append('--property='+key+'='+service[key])
        argv.append('--property=ReadWritePaths='+' '.join(str(destinations[d]) for d in writable))
        # Real private temporary file, chmod/chown, fsync, and atomic token rename
        # in the same mount namespace as the configured helper. Receipt must fail
        # to write there; both services must be denied unrelated writes.
        script='''
import errno, os, pathlib, sys, tempfile
settings, portal, embedder, outside = map(pathlib.Path, sys.argv[1:5])
(settings / 'receipt').write_text('safe')
try:
    os.chmod(portal, 0o700)
    os.chown(portal, os.getuid(), os.getgid())
    fd, name = tempfile.mkstemp(dir=portal)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write('synthetic-token')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, portal / 'test.token')
except OSError as exc:
    assert sys.argv[5] == 'receipt' and exc.errno == errno.EROFS, exc
else:
    assert sys.argv[5] == 'control'
try:
    (embedder / 'desired.json').write_text('synthetic-settings')
except OSError as exc:
    assert sys.argv[5] == 'receipt' and exc.errno == errno.EROFS, exc
else:
    assert sys.argv[5] == 'control'
try:
    (outside / 'forbidden').write_text('no')
except OSError as exc:
    assert exc.errno == errno.EROFS, exc
else:
    raise AssertionError('unrelated path became writable')
'''
        # Stdlib-only script: use a system interpreter, since ProtectHome=yes hides
        # interpreters installed under /root or /home (test venvs, uv pythons).
        interpreter=shutil.which('python3',path='/usr/bin:/bin') or os.path.realpath(sys.executable)
        argv += [interpreter,'-c',script,str(settings),str(portal),str(embedder),str(outside),kind]
        result=subprocess.run(argv,capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr
        assert (settings/'receipt').read_text()=='safe'
        assert (portal/'test.token').exists()==(kind=='control')
        assert (embedder/'desired.json').exists()==(kind=='control')
        assert not (outside/'forbidden').exists()
