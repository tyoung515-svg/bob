"""Root firewall guardian: fixed read-only status query, no caller arguments.

Every worker admission checks the live kernel rules. The guardian also stops the
whole worker cgroup/socket when rules disappear/change during an active job.
"""
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
sys.path.insert(0, '/usr/local/lib/bob_runtime')
from worker import CONFIG, policy


def main():
    lane_id = sys.argv[1]
    policy(lane_id)
    config = json.loads((CONFIG / (lane_id + '.json')).read_text())
    directory = Path('/run/bob-workers') / lane_id
    path = directory / 'check.sock'
    def snapshot():
        result = subprocess.run(['nft', 'list', 'table', 'inet', 'bob_w_' + lane_id], capture_output=True)
        return result.stdout if result.returncode == 0 else None
    expected = snapshot()
    if not expected:
        raise RuntimeError('worker firewall unavailable')
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
    path.unlink(missing_ok=True)
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            os.chown(path, config['worker_uid'], config['worker_gid'])
            path.chmod(0o600)
            listener.listen(8)
            listener.settimeout(.5)
            notify = os.environ.get('NOTIFY_SOCKET')
            if notify:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                    channel.connect('\0' + notify[1:] if notify.startswith('@') else notify)
                    channel.sendall(b'READY=1')
            while True:
                if snapshot() != expected:
                    raise RuntimeError('worker firewall changed or disappeared')
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(1)
                    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != config['worker_uid']:
                        continue
                    try:
                        if connection.recv(1) == b'?' and snapshot() == expected:
                            connection.sendall(b'1')
                    except OSError:
                        pass
    finally:
        (directory / 'policy.json').unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        subprocess.run(['systemctl', '--no-block', 'stop', f'bob-worker@{lane_id}.socket',
                        f'bob-worker-auth@{lane_id}.socket', f'bob-worker@{lane_id}.service',
                        f'bob-worker-auth@{lane_id}.service', f'bob-claude-turn@{lane_id}.socket',
                         f'bob-claude-turn@{lane_id}.service'], check=False)


if __name__ == '__main__':
    main()
