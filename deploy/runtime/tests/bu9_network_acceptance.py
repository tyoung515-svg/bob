"""Root Linux boundary check, no sign-in, tokens printed nowhere.

Run: /opt/bob/.venv/bin/python bu9_network_acceptance.py --lane setupwin
"""
import argparse
import errno
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
data=json.load(sys.stdin)
proxy=urlsplit(os.environ['HTTPS_PROXY'])
def connect(host):
    with socket.create_connection((proxy.hostname,proxy.port),timeout=8) as s:
        auth=base64.b64encode((proxy.username+':'+proxy.password).encode()).decode()
        s.sendall(('CONNECT '+host+':443 HTTP/1.1\r\nHost: '+host+':443\r\nProxy-Authorization: Basic '+auth+'\r\n\r\n').encode())
        return int(s.recv(4096).split(b' ',2)[1])
if data['mode']=='revoked':
    assert connect('auth.openai.com')==407
else:
    try:
        with socket.create_connection((data['ip'],443),timeout=3):
            raise AssertionError('direct connection unexpectedly allowed')
    except OSError as e:
        assert e.errno in {errno.EACCES,errno.EPERM,errno.ECONNREFUSED,errno.ENETUNREACH,errno.EHOSTUNREACH}
    assert connect('auth.openai.com')==200
    assert connect('example.com')==403
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane',required=True)
    args=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0:
        parser.error('This check requires Linux root and an installed worker lane.')
    sys.path.insert(0,'/usr/local/lib/bob_runtime')
    sys.path.insert(0,'/opt/bob/bobclaw-core')
    import model
    root=model.lane_root(args.lane)
    if model.execution_mode()!='worker':
        raise RuntimeError('worker mode required')
    from core.builder.network import public_address
    ip=next(r[4][0] for r in socket.getaddrinfo('auth.openai.com',443,type=socket.SOCK_STREAM)
            if r[0]==socket.AF_INET and public_address(r[4][0]))
    def check(options,mode):
        result=subprocess.run([sys.executable,'-I','-c',CHILD],
            input=json.dumps({'mode':mode,'ip':ip}),text=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25,**options)
        if result.returncode:
            raise RuntimeError('boundary assertion failed')
    with model.login_options(root,time.monotonic()+30) as options:
        check(options,'active')
    # EOF revocation is polled by the proxy every 0.1 seconds.
    time.sleep(.3)
    check(options,'revoked')
    print('PASS: worker direct TCP refused; auth.openai.com CONNECT allowed; example.com refused; closed lease refused.')


if __name__=='__main__':
    try:
        main()
    except Exception:
        print('FAIL: the installed Linux boundary check did not pass. No sign-in was attempted.',file=sys.stderr)
        sys.exit(1)
