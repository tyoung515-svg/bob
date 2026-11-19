"""Gateway-authenticated, invocation-local authority for interactive work tools.

This is not an owner session or a new token. It preserves the verified caller's
authority, and every command still enters the normal execution service checks.
"""
from contextvars import ContextVar
import hmac
import re
import time

from core.permissions import canonical_pipe_json, compute_scope_vouch

FACES = frozenset({'orchestrator', 'builder', 'fixer'})
OPERATIONS = frozenset({'capabilities', 'teams.list', 'teams.save', 'runs.start',
    'runs.get', 'runs.list', 'runs.cancel', 'jobs.attachments', 'jobs.list',
    'jobs.start', 'jobs.audit', 'jobs.cancel', 'jobs.proposal', 'jobs.artifact',
    'work.catalog', 'work.recipe.save', 'work.prepare', 'work.start', 'work.status'})
current = ContextVar('chat_execution_context', default=None)


def signature(value, secret):
    return compute_scope_vouch('bob-chat-work/v1\n' + canonical_pipe_json(value), secret)


def valid(value, proof, secret, *, owner, conversation, face, now=None):
    now = time.time() if now is None else now
    try:
        if (not isinstance(value, dict) or set(value) != {'authority','conversation_id','face_id','iat','exp'}
            or value['face_id'] not in FACES or value['face_id'] != face
            or value['conversation_id'] != conversation or value['authority']['owner'] != owner
            or type(value['iat']) is not int or type(value['exp']) is not int
            or not 0 < value['exp']-value['iat'] <= 600 or not value['iat'] <= now < value['exp']
            or not isinstance(proof,str) or not re.fullmatch('[0-9a-f]{64}',proof)):
            return False
        from .models import Authority
        auth = Authority(**value['authority'])
        if auth.session_kind or auth.audience or auth.token_type not in {'human','agent','portal'}:
            return False  # Never carry an elevated owner session into a model loop.
        return hmac.compare_digest(signature(value,secret),proof)
    except (KeyError, TypeError, ValueError):
        return False


def bind(state):
    from core.config import config
    value = state.get('work_context')
    accepted = value if valid(value,state.get('work_context_vouch'),config.BOBCLAW_SECRET,
        owner=state.get('user_id'),conversation=state.get('conversation_id'),face=state.get('face_id')) else None
    return current.set(accepted)


async def call(operation, args):
    import aiohttp
    from core.config import config
    from core.permissions import execution_vouch
    value = current.get()
    if operation not in OPERATIONS or not value or value['exp'] <= time.time():
        return {'ok':False,'status':'refused','error':{'code':'work_context_unavailable'}}
    # Match the REST work route: agents cannot prepare work or save recipes.
    if value['authority']['token_type'] == 'agent' and operation in {'work.prepare','work.recipe.save'}:
        return {'ok':False,'status':'refused','error':{'code':'not_found'}}
    if operation == 'work.prepare' and isinstance(args, dict):
        # The conversation comes only from the verified context; its model
        # preference selects the binding when it belongs to the same project.
        from . import chat_records
        try:
            import uuid
            bound = chat_records.POOL and await chat_records.POOL.fetchval(
                'SELECT project_id::text FROM conversations WHERE id=$1 AND user_id=$2',
                uuid.UUID(value['conversation_id']), value['authority']['owner'])
        except Exception:
            bound = None
        if bound and bound == str(args.get('project_id')):
            args = {**args, 'conversation_id': value['conversation_id']}
    now = int(time.time())
    envelope = {'operation':operation,'args':args,'authority':value['authority'],
                'iat':now,'exp':min(now+30,value['exp'])}
    if operation.startswith('work.'):
        envelope['face_id'] = value['face_id']
    from .chat_records import work_receipt
    try:
        await work_receipt(value,operation,{'status':'requested','args':args})
    except Exception:
        return {'ok':False,'status':'refused','error':{'code':'work_receipt_storage_unavailable'}}
    try:
        # A service-to-service hop. No bearer/provider key is exposed to the model.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            port = config.PORT
            async with session.post(f'http://127.0.0.1:{port}/api/execution/{operation}',
                    json={'envelope':envelope,'vouch':execution_vouch(envelope,config.BOBCLAW_SECRET)}) as response:
                data = await response.json()
                result={'ok':response.status < 400,'http_status':response.status,**data}
    except Exception:
        return {'ok':False,'status':'unknown','error':{'code':'work_outcome_unknown',
                'detail':'Repeat the same tool call with the same arguments and request_key to replay prepare/start/recipe.save/cancel. Do not use a new key.'}}
    # A receipt failure cannot turn a known HTTP result into an unknown outcome.
    try:
        await work_receipt(value,operation,result)
        result['receipt_saved'] = True
    except Exception:
        result['receipt_saved'] = False
    if operation == 'work.catalog' and result.get('ok'):
        from .chat_records import conversation_receipts
        try:
            result['conversation_receipts'] = await conversation_receipts(value)
            result['conversation_receipts_available'] = True
        except Exception:
            result['conversation_receipts'] = []
            result['conversation_receipts_available'] = False
    return result
