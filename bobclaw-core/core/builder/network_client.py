"""Core owns an ephemeral proxy lease during one sandbox command."""
import asyncio
import contextlib
import secrets
import socket
import struct
import time

from .egress_proxy import read_json, send_json


class JobNetwork:
    profile = 'builder'

    def __init__(self, builder, auth, row, command, job_id, config, timeout):
        self.builder, self.auth, self.row = builder, auth, row
        self.command, self.job_id, self.config, self.timeout = command, job_id, config, timeout
        self.token = secrets.token_hex(32)
        self.events, self.approvals = [], []

    async def __aenter__(self):
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_unix_connection(
            '/run/bob-egress-'+self.config['lane_id']+'/control.sock'), 5)
        try:
            _, uid, _ = struct.unpack('3i',self.writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid != self.config['egress_uid']:
                raise PermissionError('proxy identity mismatch')
            await send_json(self.writer,{'token':self.token,'job_id':self.job_id,'profile':self.profile,
                                        'expires_at':time.time()+self.timeout+20})
            if (await read_json(self.reader)).get('ready') is not True:
                raise ValueError('proxy enrollment refused')
            self.task = asyncio.create_task(self.serve())
            return self
        except BaseException:
            self.writer.close()
            raise

    @property
    def url(self):
        return f'http://bob:{self.token}@127.0.0.1:{self.config["egress_port"]}'

    async def policy(self, value):
        return await self.builder.network.policy(self.auth,self.row['conversation_id'],self.command,
            self.job_id,value['domain'],value['method'])

    async def log(self, value):
        await self.builder.network.log(self.auth,self.row,self.job_id,value)

    async def serve(self):
        while True:
            # The command may be idle for minutes; the proxy bounds lease life.
            line = await self.reader.readline()
            if not line:
                return
            import json
            value = json.loads(line)
            if value['type'] == 'policy':
                result = await self.policy(value)
                if result.get('approval') and result['approval'] not in self.approvals:
                    self.approvals.append(result['approval'])
                    # Emit the server card while the command is running, as well
                    # as retaining it in the durable mode view and tool receipt.
                    from core.tools.ask import _writer
                    from core.execution.models import canonical
                    writer = _writer.get()
                    if writer:
                        writer({'type': 'token', 'content': '```bob-builder-approval\n' +
                                canonical(result['approval']) + '\n```\n'})
            elif value['type'] == 'log':
                await self.log(value)
                self.events.append({k:v for k,v in value.items() if k!='type'})
                result = {'saved':True}
            else:
                raise ValueError('invalid proxy event')
            await send_json(self.writer,result)

    async def __aexit__(self, *exc):
        try:
            # Separate peer-checked control connection drains active tunnels and
            # their log acknowledgements while serve() still handles callbacks.
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(
                '/run/bob-egress-'+self.config['lane_id']+'/control.sock'),5)
            try:
                _, uid, _ = struct.unpack('3i',writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid != self.config['egress_uid']:
                    raise PermissionError('proxy identity mismatch')
                await send_json(writer,{'type':'revoke','token':self.token})
                if (await read_json(reader)).get('drained') is not True:
                    raise ValueError('proxy drain unproved')
            finally:
                writer.close()
            if self.task.done() and not self.task.cancelled() and self.task.exception():
                raise self.task.exception()
        finally:
            self.writer.close()
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()
