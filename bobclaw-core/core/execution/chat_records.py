"""Scoped durable operator answers and work receipts; gateway history is canonical."""
import hmac
from contextvars import ContextVar
import json
import time
import uuid

from .models import ExecutionError, canonical
from core.permissions import compute_scope_vouch

POOL = None
request_id: ContextVar[str | None] = ContextVar('chat_request_id', default=None)
RECEIPT_LIMIT = 20
RECEIPT_PAYLOAD_BYTES = 8192
RECEIPT_READ_BYTES = 65536


async def conversation_receipts(context, limit=RECEIPT_LIMIT):
    """Read bounded receipts from this verified user's conversation only."""
    if POOL is None:raise ExecutionError('work_receipt_storage_unavailable',503)
    limit = max(1,min(RECEIPT_LIMIT,int(limit)))
    rows = await POOL.fetch('''SELECT r.id,r.tool,r.created_at,
        CASE WHEN octet_length(r.payload::text) <= $4 THEN r.payload
             ELSE '{"truncated":true}'::jsonb END AS payload
        FROM chat_work_receipts r JOIN conversations c
          ON c.id=r.conversation_id AND c.user_id=r.user_id
        WHERE r.conversation_id=$1 AND r.user_id=$2
        ORDER BY r.created_at DESC,r.id DESC LIMIT $3''',
        uuid.UUID(context['conversation_id']),context['authority']['owner'],limit,RECEIPT_PAYLOAD_BYTES)
    result=[]
    for row in rows:
        payload=row['payload']
        if isinstance(payload,str):payload=json.loads(payload)
        item={'id':str(row['id']),'tool':row['tool'],'created_at':str(row['created_at']),'payload':payload}
        if len(canonical([*result,item]).encode()) > RECEIPT_READ_BYTES:break
        result.append(item)
    return result


def answer_signature(value, secret):
    return compute_scope_vouch('bob-question-answer/v1\n'+canonical(value),secret)


def valid_answer(value, proof, secret, question_id, now=None):
    now=time.time() if now is None else now
    return (isinstance(value,dict) and set(value)=={'question_id','user_id','answer','iat','exp'}
        and value['question_id']==question_id and isinstance(value['user_id'],str) and bool(value['user_id'])
        and isinstance(value['answer'],str) and 0<len(value['answer'])<=12000
        and type(value['iat']) is int and type(value['exp']) is int
        and 0<value['exp']-value['iat']<=30 and value['iat']<=now<value['exp']
        and isinstance(proof,str) and hmac.compare_digest(answer_signature(value,secret),proof))


async def question(identifier,owner,conversation,text,choices,expires, *, origin=None):
    if not owner or not conversation:
        # Unit/noninteractive callers have no durable conversation to write.
        return False
    if POOL is None:raise ExecutionError('question_storage_unavailable',503)
    async with POOL.acquire() as c, c.transaction():
        owned=await c.fetchval('SELECT id FROM conversations WHERE id=$1 AND user_id=$2',uuid.UUID(conversation),owner)
        if not owned:raise ExecutionError('conversation_not_found',404)
        await c.execute('INSERT INTO chat_questions(id,conversation_id,user_id,question,choices,expires_at,request_id,origin) VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8)',
            identifier,uuid.UUID(conversation),owner,text,canonical(choices),expires,request_id.get(),origin)
    return True


async def accept(value):
    if POOL is None:raise ExecutionError('question_storage_unavailable',503)
    async with POOL.acquire() as c, c.transaction():
        row=await c.fetchrow('SELECT q.* FROM chat_questions q JOIN conversations v ON v.id=q.conversation_id AND v.user_id=q.user_id WHERE q.id=$1 AND q.user_id=$2 FOR UPDATE OF q',value['question_id'],value['user_id'])
        if not row:raise ExecutionError('question_not_found',404)
        if row['answer'] is not None:
            if row['answer']!=value['answer']:raise ExecutionError('question_already_answered',409)
            return {'resolved':True,'replayed':True,'conversation_id':str(row['conversation_id'])}
        from core.tools.ask import pending_questions,answer_question
        if row['expires_at']<=time.time() or row['id'] not in pending_questions():
            raise ExecutionError('question_expired',409)
        content='Operator answer to: '+row['question']+'\n'+value['answer']
        identifier=uuid.uuid5(uuid.NAMESPACE_URL,'bob-question:'+row['id'])
        metadata = {'kind':'operator_answer','question_id':row['id']}
        if row.get('origin'):
            metadata['origin'] = row['origin']
        if row.get('request_id'):
            metadata['request_id'] = row['request_id']
        await c.execute("INSERT INTO messages(id,conversation_id,role,content,metadata) VALUES($1,$2,'user',$3,$4::jsonb) ON CONFLICT(id) DO NOTHING",
            identifier,row['conversation_id'],content,canonical(metadata))
        await c.execute('UPDATE chat_questions SET answer=$2,answered_at=now() WHERE id=$1',row['id'],value['answer'])
    # Resolve only after the accepted answer and history row are committed.
    delivered=answer_question(row['id'],value['answer'])
    return {'resolved':True,'delivered_to_turn':delivered,'message_id':str(identifier),'conversation_id':str(row['conversation_id'])}


async def work_receipt(context,operation,result):
    if POOL is None:raise ExecutionError('work_receipt_storage_unavailable',503)
    # Work results contain scoped catalog/receipt data, never credentials.
    raw=canonical(result)
    payload=result if len(raw.encode())<=65536 else {'truncated':True,'operation':operation,
        'job_id':result.get('id'),'run_id':result.get('run_id'),'preparation_id':result.get('preparation_id')}
    identifier=await POOL.fetchval('INSERT INTO chat_work_receipts(conversation_id,user_id,tool,payload) SELECT id,user_id,$3,$4::jsonb FROM conversations WHERE id=$1 AND user_id=$2 RETURNING id',
        uuid.UUID(context['conversation_id']),context['authority']['owner'],operation,canonical(payload))
    if identifier is None:raise ExecutionError('conversation_not_found',404)
