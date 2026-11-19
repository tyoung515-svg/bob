"""CC1 reuses BU9's root-only lease admission and EOF revocation."""
import asyncio
import json
import socket
import struct
import time
from types import SimpleNamespace
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,kind,profile,accepted', [
    (0, 'setup-claude', 'claude', True), (42, 'setup-claude', 'claude', False),
    (99, 'setup-claude', 'claude', False), (0, 'setup-codex', 'claude', False),
    (0, 'setup-claude', 'codex', False), (0, 'setup-claude', 'mcp', False),
])
async def test_root_only_matched_profile_and_eof(monkeypatch, uid, kind, profile, accepted):
    from core.builder.egress_proxy import Proxy
    monkeypatch.setattr(socket, 'SO_PEERCRED', 17, raising=False)
    reader = asyncio.StreamReader()
    class Writer:
        def __init__(self): self.data = b''; self.closed = False
        def get_extra_info(self, key):
            return SimpleNamespace(getsockopt=lambda *args: struct.pack('3i', 123, uid, uid))
        def write(self, data): self.data += data
        async def drain(self): pass
        def close(self): self.closed = True
        async def wait_closed(self): pass
    writer = Writer()
    proxy = Proxy(42)
    reader.feed_data((json.dumps(dict(type=kind, profile=profile, token='a' * 64,
                                     expires_at=time.time() + 30)) + '\n').encode())
    task = asyncio.create_task(proxy.control(reader, writer))
    await asyncio.sleep(.01)
    assert bool(proxy.leases) is accepted
    if accepted:
        lease = next(iter(proxy.leases.values()))
        tunnel = asyncio.create_task(asyncio.Event().wait())
        lease.tasks.add(tunnel)
    reader.feed_eof()
    await asyncio.wait_for(task, 1)
    assert not proxy.leases and writer.closed
    if accepted:
        await asyncio.sleep(0)
        assert lease.closed and tunnel.cancelled()
