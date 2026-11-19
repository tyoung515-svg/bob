"""Durable Windows requests. No grants, host execution or approval API in WSL."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import time
import uuid

from .models import Authority, ExecutionError, only, request_key
from .store import decoded

OPERATIONS = frozenset({'pc.run', 'pc.read', 'pc.write', 'pc.list', 'pc.open', 'pc.info'})


async def end_conversation(connection,lane,owner,conversation):
    await connection.execute('''UPDATE pc_tasks SET ended_at=$4 WHERE lane=$1 AND owner=$2
        AND conversation_id=$3 AND ended_at IS NULL''',lane,owner,conversation,time.time())
    await connection.execute('''UPDATE pc_requests r SET state=CASE WHEN r.state='running' THEN 'unknown' ELSE 'stopped' END
        FROM pc_tasks t WHERE r.task_id=t.id AND t.lane=$1 AND t.owner=$2
        AND t.conversation_id=$3 AND r.state IN('pending','running')''',lane,owner,conversation)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(operation, args):
    fields = {'pc.run': {'shell', 'argv', 'cwd', 'timeout_s'}, 'pc.read': {'path'},
              'pc.write': {'path', 'content', 'preimage_sha256'}, 'pc.list': {'path'}, 'pc.open': {'path'},
              'pc.info': set()}
    if operation not in fields:
        raise ExecutionError('unsupported_pc_operation')
    only(args, fields[operation])
    if operation == 'pc.info':
        return args
    if operation == 'pc.run':
        if (args.get('shell') not in {'powershell', 'cmd'} or not isinstance(args.get('argv'), list)
                or not 1 <= len(args['argv']) <= 64
                or any(not isinstance(s, str) or len(s) > 8192 or '\x00' in s for s in args['argv'])
                or type(args.get('timeout_s')) is not int or not 1 <= args['timeout_s'] <= 600):
            raise ExecutionError('invalid_pc_command')
        path = args.get('cwd')
    else:
        path = args.get('path')
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(ord(c) < 32 for c in path):
        raise ExecutionError('invalid_pc_path')
    if operation == 'pc.write':
        if not isinstance(args.get('content'), str) or len(args['content'].encode()) > 128 * 1024:
            raise ExecutionError('pc_write_too_large')
        preimage = args.get('preimage_sha256')
        if preimage is not None and (not isinstance(preimage, str) or not re.fullmatch('[0-9a-f]{64}', preimage)):
            raise ExecutionError('invalid_pc_preimage')
    return args


def verify_native(payload, signature, *, key_path=Path('/etc/bob/pc-host-public.json')):
    """RSA/SHA256 PKCS#1 v1.5, verifying a root-owned native-enrolled public key.

    The private key never leaves the Windows process. No runtime crypto provider
    or key-enrollment endpoint is needed in core, gateway or broker.
    """
    try:
        info = key_path.lstat()
        parent = key_path.parent.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > 8192:
            return False
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
            return False
        key = json.loads(key_path.read_text())
        blob = bytes.fromhex(key['public_blob'])
        magic, bits, exponent_size, modulus_size, p, q = struct.unpack('<6I', blob[:24])
        if (magic != 0x31415352 or bits != 2048 or modulus_size != 256 or not 1 <= exponent_size <= 8
                or p or q or len(blob) != 24 + exponent_size + modulus_size):
            return False
        if payload.get('host_id') != hashlib.sha256(blob).hexdigest():
            return False
        now = time.time()
        if (type(payload.get('iat')) is not int or type(payload.get('exp')) is not int
                or not 0 < payload['exp'] - payload['iat'] <= 60 or not payload['iat'] <= now < payload['exp']):
            return False
        e = int.from_bytes(blob[24:24+exponent_size], 'big')
        n = int.from_bytes(blob[24+exponent_size:], 'big')
        signed = bytes.fromhex(signature)
        if len(signed) != modulus_size or int.from_bytes(signed, 'big') >= n or e < 3 or e % 2 == 0:
            return False
        result = pow(int.from_bytes(signed, 'big'), e, n).to_bytes(modulus_size, 'big')
        expected_digest = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(canonical(payload).encode()).digest()
        expected = b'\x00\x01' + b'\xff' * (modulus_size - len(expected_digest) - 3) + b'\x00' + expected_digest
        import hmac
        return hmac.compare_digest(result, expected)
    except (OSError, ValueError, KeyError, TypeError, struct.error, OverflowError):
        return False


class PC:
    def __init__(self, pool):
        self.pool = pool

    async def task(self, auth, agent, conversation):
        conversation = uuid.UUID(conversation)
        async with self.pool.acquire() as c, c.transaction():
            if not await c.fetchval('SELECT id FROM conversations WHERE id=$1 AND user_id=$2', conversation, auth.owner):
                raise ExecutionError('conversation_not_found', 404)
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)', int(digest([auth.lane, auth.owner, agent, str(conversation)])[:15], 16))
            row = await c.fetchrow('''SELECT * FROM pc_tasks WHERE lane=$1 AND owner=$2 AND agent=$3
                AND principal=$4 AND conversation_id=$5 AND credential_epoch=$6 AND ended_at IS NULL''',
                auth.lane, auth.owner, agent, auth.actor, conversation, auth.credential_epoch)
            if row:
                return dict(row)
            return dict(await c.fetchrow('''INSERT INTO pc_tasks
                (id,lane,owner,agent,principal,conversation_id,credential_epoch,created_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *''', uuid.uuid4().hex, auth.lane, auth.owner,
                agent, auth.actor, conversation, auth.credential_epoch, time.time()))

    async def request(self, context, operation, args, key, writer=None):
        auth = Authority(**context['authority'])
        validate(operation, args)
        task = await self.task(auth, context['face_id'], context['conversation_id'])
        key = request_key(key)
        row = await self.pool.fetchrow('''INSERT INTO pc_requests
            (id,task_id,operation,args,args_sha256,request_key,created_at,expires_at)
            VALUES($1,$2,$3,$4::jsonb,$5,$6,$7,$8) ON CONFLICT(task_id,request_key)
            DO UPDATE SET request_key=EXCLUDED.request_key RETURNING *''', uuid.uuid4().hex, task['id'],
            operation, canonical(args), digest([operation,args]), key, time.time(), time.time()+600)
        if row['args_sha256'] != digest([operation,args]):
            raise ExecutionError('pc_request_key_conflict',409)
        ticks = 0
        try:
            while row['state'] in {'pending','running'} and time.time() < row['expires_at']:
                if writer and ticks % 10 == 0:
                    writer({'type':'builder_wait','approval_id':row['id'],'waiting':True,'expires_at':row['expires_at'],
                            'conversation_id':context['conversation_id'],'agent':context['face_id']})
                await asyncio.sleep(.5)
                row = await self.pool.fetchrow('SELECT * FROM pc_requests WHERE id=$1', row['id'])
                ticks += 1
            if row['state'] in {'pending','running'}:
                row = await self.pool.fetchrow('''UPDATE pc_requests SET state=CASE WHEN state='running' THEN 'unknown' ELSE 'expired' END
                    WHERE id=$1 AND state IN ('pending','running') RETURNING *''', row['id']) or row
            return {'request_id':row['id'],'task_id':task['id'],'status':row['state'],
                    'result':decoded(row['receipt']), 'host_execution':'native_windows_only',
                    'detail':('Unknown mutations are never replayed; inspect the native receipt before proposing another action.' if row['state']=='unknown'
                        else 'The user did not approve this request in the Windows app within 10 minutes. Nothing ran; nothing is wedged. Before retrying, tell the user the approval appears in the Windows app, under Needs you.' if row['state']=='expired' and not row.get('host_id') else None)}
        finally:
            if writer:
                writer({'type':'builder_wait','approval_id':row['id'],'waiting':False,'expires_at':row['expires_at'],
                        'conversation_id':context['conversation_id'],'agent':context['face_id']})

    async def command(self, auth, operation, args):
        # These collection/control operations belong to the native portal's
        # existing scoped connection. A delegated agent may propose its own PC
        # request through vouched chat, but must not enumerate other tasks.
        if auth.token_type!='portal' or auth.actor!=auth.owner:
            raise ExecutionError('native_portal_required',403)
        if operation=='pc.end':
            only(args,{'conversation_id'})
            conversation=uuid.UUID(args['conversation_id'])
            async with self.pool.acquire() as c,c.transaction():
                if not await c.fetchval('SELECT id FROM conversations WHERE id=$1 AND user_id=$2',conversation,auth.owner):
                    raise ExecutionError('conversation_not_found',404)
                await c.execute('''UPDATE pc_tasks SET ended_at=$4 WHERE lane=$1 AND owner=$2
                    AND conversation_id=$3 AND ended_at IS NULL''',auth.lane,auth.owner,conversation,time.time())
                await c.execute('''UPDATE pc_requests r SET state=CASE WHEN r.state='running' THEN 'unknown' ELSE 'stopped' END
                    FROM pc_tasks t WHERE r.task_id=t.id AND t.lane=$1 AND t.owner=$2
                    AND t.conversation_id=$3 AND r.state IN('pending','running')''',auth.lane,auth.owner,conversation)
            return {'stopped':True}
        if operation == 'pc.pending':
            only(args,set())
            await self.pool.execute('''UPDATE pc_requests r SET state=CASE WHEN r.state='running' THEN 'unknown' ELSE 'expired' END
                FROM pc_tasks t WHERE r.task_id=t.id AND t.lane=$1 AND t.owner=$2
                AND r.state IN('pending','running') AND r.expires_at<=$3''',auth.lane,auth.owner,time.time())
            rows=await self.pool.fetch('''SELECT r.*,t.lane,t.owner,t.agent,t.principal,t.conversation_id,t.credential_epoch
                FROM pc_requests r JOIN pc_tasks t ON t.id=r.task_id WHERE t.lane=$1 AND t.owner=$2
                AND r.state='pending' AND t.ended_at IS NULL AND t.credential_epoch=$3 ORDER BY r.created_at LIMIT 50''',auth.lane,auth.owner,auth.credential_epoch)
            ended=await self.pool.fetch('''SELECT id FROM pc_tasks WHERE lane=$1 AND owner=$2
                AND (ended_at IS NOT NULL OR credential_epoch<>$3) ORDER BY created_at DESC LIMIT 1000''',auth.lane,auth.owner,auth.credential_epoch)
            return {'requests':[self.wire(r) for r in rows],'ended_tasks':[r['id'] for r in ended],
                    'credential_epoch':auth.credential_epoch,'lane':auth.lane,'owner':auth.owner}
        if operation not in {'pc.claim','pc.receipt'}:
            raise ExecutionError('native_approval_required',403)
        only(args,{'payload','signature'})
        payload=args.get('payload')
        if not isinstance(payload,dict) or not verify_native(payload,args.get('signature')):
            raise ExecutionError('native_signature_required',403)
        fields={'kind','request_id','task_id','args_sha256','host_id','iat','exp','nonce'}
        only(payload, fields | ({'result'} if operation=='pc.receipt' else set()))
        if payload.get('kind') != operation or not re.fullmatch('[0-9a-f]{32}',str(payload.get('nonce',''))):
            raise ExecutionError('invalid_native_receipt',403)
        async with self.pool.acquire() as c,c.transaction():
            row=await c.fetchrow('''SELECT r.*,t.lane,t.owner,t.agent,t.principal,t.conversation_id,t.credential_epoch,t.ended_at
                FROM pc_requests r JOIN pc_tasks t ON t.id=r.task_id WHERE r.id=$1 AND t.lane=$2 AND t.owner=$3 FOR UPDATE OF r''',
                payload.get('request_id'),auth.lane,auth.owner)
            if (not row or row['task_id']!=payload.get('task_id') or row['args_sha256']!=payload.get('args_sha256')
                    or row['ended_at'] is not None or row['credential_epoch']!=auth.credential_epoch):
                raise ExecutionError('pc_task_ended',409)
            if operation=='pc.claim':
                if row['state']!='pending' or row['expires_at']<=time.time():
                    raise ExecutionError('pc_request_not_pending',409)
                timeout=decoded(row['args']).get('timeout_s',30) if row['operation']=='pc.run' else 30
                await c.execute("UPDATE pc_requests SET state='running',host_id=$2,claim_nonce=$3,expires_at=$4 WHERE id=$1",row['id'],payload['host_id'],payload['nonce'],time.time()+timeout+60)
                return {'claimed':True,'request_id':row['id']}
            result=payload.get('result')
            if (not isinstance(result,dict) or result.get('status') not in {'done','failed','denied','expired','stopped','unknown'}
                    or len(canonical(result).encode())>192*1024):
                raise ExecutionError('invalid_native_receipt')
            if row['host_id']!=payload['host_id']:
                raise ExecutionError('native_host_changed',403)
            if row['state']!='running':
                if decoded(row['receipt'])==args:
                    return {'recorded':True,'request_id':row['id']}
                raise ExecutionError('pc_request_terminal',409)
            await c.execute('UPDATE pc_requests SET state=$2,receipt=$3::jsonb WHERE id=$1',row['id'],result['status'],canonical(args))
            return {'recorded':True,'request_id':row['id']}

    @staticmethod
    def wire(row):
        return {key:(str(value) if key=='conversation_id' else decoded(value) if key=='args' else value)
                for key,value in dict(row).items() if key in {'id','task_id','operation','args','args_sha256','expires_at',
                    'lane','owner','agent','principal','conversation_id','credential_epoch'}}
