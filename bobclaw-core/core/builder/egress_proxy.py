"""Stdlib lane HTTP/CONNECT proxy, with per-job service-UID enrollment.

The Unix control channel is SO_PEERCRED checked in production. Closing it revokes
the token and cancels its tunnels. Only core chooses policy and saves receipts.
No bearer/provider key is ever stored in runtime configuration.
"""
import asyncio
import base64
import contextlib
import json
import os
import re
import socket
import struct
import time
from urllib.parse import urlsplit

from .network import DEFAULTS, domain, public_address

HEADER_LIMIT = 32768
BYTE_LIMIT = 64 * 1024 * 1024


async def send_json(writer, value):
    writer.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
    await writer.drain()


async def read_json(reader):
    raw = await asyncio.wait_for(reader.readline(), 10)
    if not raw or len(raw) > HEADER_LIMIT:
        raise ValueError('control channel unavailable')
    return json.loads(raw)


async def public_connect(host, port):
    # Resolve once, validate ALL answers, and connect to a numeric address. No
    # second resolution at connect time (DNS rebinding / private-host SSRF).
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not rows or any(not public_address(r[4][0]) for r in rows):
        raise ValueError('private or reserved upstream refused')
    last = None
    for family, _, _, _, address in rows:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address[0], port, family=family), 10)
        except OSError as exc:
            last = exc
    raise OSError('upstream unavailable') from last


class Lease:
    def __init__(self, reader, writer, expiry, job_id=None, profile='builder', setup=False):
        self.reader, self.writer, self.expiry = reader, writer, expiry
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.requests = 0
        self.closed = False
        self.job_id = job_id
        self.profile = profile
        self.revoking = False
        self.setup = setup
        self.setup_deadline = time.monotonic() + max(0, expiry-time.time()) if setup else None

    def expired(self):
        deadline = getattr(self, 'setup_deadline', None)
        return time.time() >= self.expiry or deadline is not None and time.monotonic() >= deadline

    async def event(self, value):
        if self.setup:
            # Root setup cannot supply policy callbacks or extend the deadline.
            if self.closed or self.expired():
                raise ValueError('setup lease expired')
            if value['type'] == 'policy':
                return {'decision': 'allow' if value['method'] == 'CONNECT' and
                        value['domain'] in DEFAULTS['profiles'][self.profile] else 'deny'}
            return {'saved': True}  # request() journals sanitized metadata
        async def exchange():
            async with self.lock:
                if self.closed or self.expired():
                    raise ValueError('job token expired')
                await send_json(self.writer, value)
                return await read_json(self.reader)
        # A cancelled tunnel must not abandon a half-read policy response and
        # let its final log consume that response as a save acknowledgement.
        # The exchange owns the lock until its bounded response finishes.
        task = asyncio.create_task(exchange())
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)


class Proxy:
    def __init__(self, service_uid, *, connector=public_connect, max_timeout=640):
        self.service_uid, self.connector = service_uid, connector
        self.max_timeout = max_timeout
        self.leases = {}

    async def control(self, reader, writer):
        token, lease = None, None
        try:
            sock = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in {0, self.service_uid}:
                return
            value = await read_json(reader)
            setup = value.get('type') in {'setup-codex', 'setup-claude'}
            if (setup and uid != 0) or (not setup and uid != self.service_uid):
                return
            if value.get('type') == 'revoke':
                current = self.leases.get(value.get('token'))
                if current:
                    current.revoking = True
                    tasks = list(current.tasks)
                    for task in tasks:
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),8)
                    current.closed = True
                await send_json(writer,{'drained':True})
                return
            token, expiry = value['token'], value['expires_at']
            profile = value.get('profile', 'builder')
            limit = 900 if setup else 660 if profile == 'builder' else self.max_timeout + 20
            if (not isinstance(token, str) or len(token) != 64 or token in self.leases
                    or profile not in {'builder', 'codex', 'claude', 'mcp'}
                    or setup and (value['type'] != 'setup-' + profile or profile not in {'codex', 'claude'} or set(value) != {'type','token','expires_at','profile'})
                    or type(expiry) not in (int, float)
                    or not time.time() < expiry <= time.time() + limit):
                raise ValueError('invalid enrollment')
            lease = self.leases[token] = Lease(reader, writer, expiry, value.get('job_id'), profile, setup)
            await send_json(writer, {'ready': True})
            # Request handlers own reads under lease.lock. EOF is monitored
            # without racing those reads; expiry always revokes active tunnels.
            while not reader.at_eof() and not lease.closed and not lease.expired():
                await asyncio.sleep(.1)
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            pass
        finally:
            if lease:
                lease.closed = True
                self.leases.pop(token, None)
                for task in list(lease.tasks):
                    task.cancel()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def request(self, reader, writer):
        lease, upstream = None, None
        record = {'domain': '', 'method': '', 'bytes_in': 0, 'bytes_out': 0, 'decision': 'deny'}
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            if len(raw) > HEADER_LIMIT:
                raise ValueError('headers too large')
            lines = raw.decode('latin1').split('\r\n')
            method, target, version = lines[0].split(' ')
            if any(ord(c) < 33 or ord(c) == 127 for c in target):
                raise ValueError('invalid request target')
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,32}", method) or version != 'HTTP/1.1':
                raise ValueError('unsupported HTTP request')
            headers = {}
            for line in lines[1:-2]:
                name, value = line.split(':', 1)
                if (not name or name.strip() != name or not name.isascii()
                        or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in name)
                        or name.lower() in headers or any(ord(c) < 32 and c != '\t' for c in value)):
                    raise ValueError('ambiguous headers')
                headers[name.lower()] = value.strip()
            auth = headers.pop('proxy-authorization', '')
            try:
                scheme, encoded = auth.split(' ', 1)
                user, token = base64.b64decode(encoded, validate=True).decode().split(':', 1)
                if scheme.lower() != 'basic' or user != 'bob':
                    raise ValueError()
                lease = self.leases.get(token)
            except (ValueError, UnicodeError):
                lease = None
            if not lease or lease.closed or lease.revoking or Lease.expired(lease):
                print(json.dumps({**record,'decision':'token_required'}),flush=True)
                lease = None  # Unauthenticated attempts have only a journal receipt.
                await self.error(writer, 407, 'bob-proxy: job token required')
                return
            lease.requests += 1
            if lease.requests > 512 or len(lease.tasks) >= 16:
                raise ValueError('job proxy request limit')
            lease.tasks.add(asyncio.current_task())
            record['job_id'] = lease.job_id
            record['profile'] = lease.profile
            url = urlsplit('https://' + target if method == 'CONNECT' else target)
            if url.username or url.password or url.fragment or url.scheme != ('https' if method == 'CONNECT' else 'http'):
                raise ValueError('invalid proxy target')
            host, port = domain(url.hostname), url.port or (443 if method == 'CONNECT' else 80)
            if port != (443 if method == 'CONNECT' else 80) or method == 'CONNECT' and (url.path or url.query):
                raise ValueError('upstream port refused')
            record.update(domain=host, method=method)
            if 'transfer-encoding' in headers or 'upgrade' in headers or 'expect' in headers:
                raise ValueError('streamed request bodies/upgrades unsupported')
            size = int(headers.get('content-length', '0'))
            if size < 0 or size > BYTE_LIMIT or method == 'CONNECT' and size:
                raise ValueError('body too large')
            decision = await lease.event({'type': 'policy', 'domain': host, 'method': method})
            record.update(decision=decision['decision'], approval_id=decision.get('approval_id'))
            if decision['decision'] != 'allow':
                message = ('bob-proxy: domain ' + host + ' needs approval (approval_id ' +
                           str(decision.get('approval_id', 'unavailable')) + ')'
                           if decision['decision'] == 'ask' else 'bob-proxy: domain ' + host + ' denied')
                await self.error(writer, 403, message)
                return
            remote_reader, upstream = await self.connector(host, port)
            if method == 'CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                async def pump(source, dest, counter):
                    while True:
                        data = await asyncio.wait_for(source.read(65536), 60)
                        if not data:
                            return
                        record[counter] += len(data)
                        if record[counter] > BYTE_LIMIT:
                            raise ValueError('tunnel byte limit')
                        dest.write(data)
                        await dest.drain()
                tasks = [asyncio.create_task(pump(reader, upstream, 'bytes_out')),
                         asyncio.create_task(pump(remote_reader, writer, 'bytes_in'))]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            task.result()
                        except (ConnectionResetError, BrokenPipeError):
                            # TLS clients commonly close with a reset once they are
                            # done (e.g. unread session tickets). After traffic has
                            # flowed both ways that is an ordinary close, not a
                            # proxy error; an immediate reset still counts as error.
                            if not (record['bytes_in'] and record['bytes_out']):
                                raise
                            record['close'] = 'reset'
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                # One request per connection, origin-form upstream. Strip hop
                # headers including names declared by Connection (smuggling).
                connection = {h.strip().lower() for h in headers.get('connection', '').split(',')}
                headers = {k:v for k,v in headers.items() if k not in connection | {
                    'connection','proxy-connection','keep-alive','te','trailer','host','proxy-authenticate'}}
                headers.update(host=host, connection='close', **{'content-length': str(size)})
                path = url.path or '/'
                if url.query:
                    path += '?' + url.query
                request = (method + ' ' + path + ' HTTP/1.1\r\n' + ''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n').encode('latin1')
                upstream.write(request)
                await upstream.drain()
                record['bytes_out'] += len(request)
                while size:
                    data = await asyncio.wait_for(reader.read(min(size, 65536)), 30)
                    if not data:
                        raise ValueError('short request body')
                    size -= len(data)
                    record['bytes_out'] += len(data)
                    upstream.write(data)
                    await upstream.drain()
                while True:
                    data = await asyncio.wait_for(remote_reader.read(65536), 60)
                    if not data:
                        break
                    record['bytes_in'] += len(data)
                    if record['bytes_in'] > BYTE_LIMIT:
                        raise ValueError('response byte limit')
                    writer.write(data)
                    await writer.drain()
        except asyncio.CancelledError:
            record['decision'] = 'cancelled'
            raise
        except (OSError, ValueError, KeyError, TypeError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, asyncio.TimeoutError):
            record['decision'] = 'error'
            with contextlib.suppress(OSError):
                await self.error(writer, 502, 'bob-proxy: request refused or upstream unavailable')
        finally:
            if upstream:
                upstream.close()
            if lease:
                # Each request is journaled too, even if core disappears before
                # it can accept its durable receipt. Never log the job token.
                print(json.dumps(record), flush=True)
                with contextlib.suppress(OSError, ValueError, asyncio.TimeoutError, asyncio.CancelledError):
                    saved = await lease.event({'type': 'log', **record})
                    if saved.get('saved') is not True:
                        raise ValueError('network receipt not acknowledged')
                lease.tasks.discard(asyncio.current_task())
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    @staticmethod
    async def error(writer, status, message):
        data = (message + '\n').encode()
        writer.write(f'HTTP/1.1 {status} Bob Proxy\r\nConnection: close\r\nProxy-Authenticate: Basic realm="bob-builder"\r\nContent-Length: {len(data)}\r\n\r\n'.encode() + data)
        await writer.drain()


async def main(config):
    path = '/run/bob-egress-' + config['lane_id'] + '/control.sock'
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    proxy = Proxy(config['service_uid'], max_timeout=config.get('max_timeout', 640))
    control = await asyncio.start_unix_server(proxy.control, path, limit=HEADER_LIMIT)
    os.chmod(path, 0o666)  # authority is the kernel peer UID, never path visibility
    server = await asyncio.start_server(proxy.request, '127.0.0.1', config['egress_port'], limit=HEADER_LIMIT)
    notify = os.environ.get('NOTIFY_SOCKET')
    if notify:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
            sock.connect('\0'+notify[1:] if notify.startswith('@') else notify)
            sock.sendall(b'READY=1')
    async with control, server:
        await asyncio.gather(control.serve_forever(), server.serve_forever())


if __name__ == '__main__':
    import sys
    from core.worker.contract import root_json
    asyncio.run(main(root_json(__import__('pathlib').Path(sys.argv[1]))))
