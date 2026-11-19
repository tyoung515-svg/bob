"""Invocation-local request authority for every agent; never a Windows grant."""
from contextvars import ContextVar
import hmac
import json
import re
import time
from core.permissions import compute_scope_vouch

current = ContextVar('pc_request_context', default=None)


def signature(value, secret):
    return compute_scope_vouch('bob-chat-pc/v1\n' + json.dumps(value, sort_keys=True, separators=(',', ':')), secret)


def bind(state):
    from core.config import config
    from core.execution.models import Authority
    value, proof = state.get('pc_context'), state.get('pc_context_vouch')
    try:
        auth = Authority(**value['authority'])
        valid = (set(value) == {'authority', 'conversation_id', 'face_id', 'iat', 'exp'}
                 and (value['face_id']=='*' or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['face_id']))
                 and (value['face_id']=='*' or value['face_id'] == state.get('face_id'))
                 and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',state.get('face_id',''))
                 and value['conversation_id'] == state.get('conversation_id')
                 and auth.owner == state.get('user_id') and not auth.session_kind and not auth.audience
                 and type(auth.credential_epoch) is int and type(value['iat']) is int and type(value['exp']) is int
                 and 0 < value['exp'] - value['iat'] <= 600 and value['iat'] <= time.time()
                 and isinstance(proof, str) and hmac.compare_digest(signature(value, config.BOBCLAW_SECRET), proof))
    except (TypeError, ValueError, KeyError):
        valid = False
    # The gateway may authorize server-selected Auto routing, never a model-
    # supplied identity. Bind its vouched caller to the actual executing face.
    # Retain an authenticated expired context for diagnosis only; invoke rejects
    # it before any PC operation. Never refresh its original expiry.
    return current.set({**value,'face_id':state['face_id']} if valid else None)
