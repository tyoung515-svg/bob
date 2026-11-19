"""Installed Linux/root Claude proxy boundary probe. No login or model request."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

CHILD = r'''
import base64, errno, json, os, socket, sys
from urllib.parse import urlsplit
value = json.load(sys.stdin)
proxy = urlsplit(os.environ['HTTPS_PROXY'])
def connect(host):
    with socket.create_connection((proxy.hostname, proxy.port), timeout=8) as channel:
        auth = base64.b64encode((proxy.username + ':' + proxy.password).encode()).decode()
        channel.sendall(('CONNECT ' + host + ':443 HTTP/1.1\r\nHost: ' + host +
                        ':443\r\nProxy-Authorization: Basic ' + auth + '\r\n\r\n').encode())
        return int(channel.recv(4096).split(b' ', 2)[1])
assert os.environ['DISABLE_AUTOUPDATER'] == '1'
assert os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'
if value['revoked']:
    assert connect('api.anthropic.com') == 407
else:
    try:
        with socket.create_connection((value['ip'], 443), timeout=3):
            raise AssertionError('direct TCP unexpectedly allowed')
    except OSError as error:
        assert error.errno in {errno.EACCES, errno.EPERM, errno.ECONNREFUSED,
                               errno.ENETUNREACH, errno.EHOSTUNREACH}
    for host in value['hosts']:
        assert connect(host) == 200
    for host in ('example.com', 'downloads.claude.ai', 'mcp-proxy.anthropic.com'):
        assert connect(host) == 403
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane', required=True)
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Linux root and an installed worker lane are required')
    sys.path.insert(0, '/usr/local/lib/bob_runtime')
    import model
    from core.builder.network import DEFAULTS, public_address
    root = model.signin_root(args.lane)
    config = model.prepare_claude(root)
    hosts = DEFAULTS['profiles']['claude']
    ip = next(row[4][0] for row in socket.getaddrinfo('api.anthropic.com', 443, type=socket.SOCK_STREAM)
              if row[0] == socket.AF_INET and public_address(row[4][0]))
    def check(options, revoked):
        result = subprocess.run([sys.executable, '-I', '-c', CHILD],
            input=json.dumps(dict(hosts=hosts, ip=ip, revoked=revoked)), text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, **options)
        if result.returncode:
            raise RuntimeError('Claude boundary assertion failed; child output withheld')
    with model.claude_setup_options(root, config, time.monotonic() + 75) as options:
        check(options, False)
    time.sleep(.3)
    check(options, True)
    print('PASS: Claude setup proxy hosts allowed; direct TCP, optional hosts and closed lease refused. No sign-in attempted.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('FAIL: installed Claude boundary probe did not pass; output withheld.', file=sys.stderr)
        sys.exit(1)
