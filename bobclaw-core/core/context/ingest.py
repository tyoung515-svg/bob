"""Authenticated gateway handoff of canonical stored conversation pages."""
import hmac
import time
from pathlib import Path
from uuid import UUID

from core.execution.models import canonical
from core.permissions import compute_scope_vouch

PAGE_SIZE = 100
# Keep a page below aiohttp's default 1 MiB request ceiling even with JSON
# escaping. Large message bodies are resolved by exact stored id at ingress.
INLINE_ROW_BYTES = 6000


def signature(value, secret):
    return compute_scope_vouch('bob-conversation-index/v1\n' + canonical(value), secret)


def valid(value, proof, secret, lane, now=None):
    now = time.time() if now is None else now
    try:
        if (not isinstance(value, dict) or set(value) != {
                'lane', 'user_id', 'conversation_id', 'rows', 'offset', 'previous_id', 'iat', 'exp'}
                or value['lane'] != lane or not isinstance(value['user_id'], str) or not value['user_id']
                or type(value['iat']) is not int or type(value['exp']) is not int
                or not 0 < value['exp'] - value['iat'] <= 30 or not value['iat'] <= now < value['exp']
                or type(value['offset']) is not int or value['offset'] < 0
                or not isinstance(value['previous_id'], str)
                or not isinstance(value['rows'], list) or not 0 < len(value['rows']) <= PAGE_SIZE
                or not isinstance(proof, str) or not proof.isascii()):
            return False
        UUID(value['conversation_id'])
        for row in value['rows']:
            if (not isinstance(row, dict) or set(row) != {'id', 'role', 'content', 'created_at'}
                    or row['role'] not in {'user', 'assistant', 'system', 'tool'}
                    or (row['content'] is not None and not isinstance(row['content'], str))
                    or not isinstance(row['created_at'], str)):
                return False
            UUID(row['id'])
        return hmac.compare_digest(signature(value, secret), proof)
    except (ValueError, TypeError, AttributeError):
        return False


async def receive(body, secret):
    """Recheck lane confinement and canonical ownership before any index write."""
    import asyncio
    from core.context import store
    from core.execution import chat_records
    from core.execution.api import lane_id
    from core.lane_config import configured_mode, get_lane

    if not isinstance(body, dict) or not valid(body.get('envelope'), body.get('vouch'), secret, lane_id()):
        return 403, {'error': 'invalid_index_vouch'}
    if not configured_mode() or get_lane().owned_path(str(Path(store.db_path()).resolve())) is None:
        return 403, {'error': 'lane_scope_unavailable'}
    value = body['envelope']
    if chat_records.POOL is None:
        return 503, {'error': 'history_unavailable'}
    owned = await chat_records.POOL.fetchval(
        'SELECT id FROM conversations WHERE id=$1 AND user_id=$2 AND is_archived=FALSE',
        UUID(value['conversation_id']), value['user_id'], timeout=5)
    if owned is None:
        return 404, {'error': 'conversation_not_found'}
    # Independent import: never await it or let its admission fail this write.
    try:
        from core.context.backfill import ensure_started
        ensure_started(value['user_id'])
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Earlier conversation backfill could not start', exc_info=True)
    rows = value['rows']
    large_ids = [UUID(row['id']) for row in rows if row['content'] is None]
    if large_ids:
        # Fetch only the explicitly handed-off ids, with the owner check in
        # the same query. This is internal ingestion, never a tool search.
        stored = await chat_records.POOL.fetch('''SELECT m.id, m.role, m.content, m.created_at
            FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE c.id=$1 AND c.user_id=$2 AND c.is_archived=FALSE
              AND m.id=ANY($3::uuid[])''',
            UUID(value['conversation_id']), value['user_id'], large_ids, timeout=5)
        resolved = {str(row['id']): dict(id=str(row['id']), role=row['role'],
                    content=row['content'] or '', created_at=row['created_at'].isoformat()) for row in stored}
        if any(str(identifier) not in resolved for identifier in large_ids):
            return 404, {'error': 'message_not_found'}
        rows = [resolved[row['id']] if row['content'] is None else row for row in rows]
    await asyncio.to_thread(store.record_stored_history, value['conversation_id'], value['user_id'],
                            rows, value['offset'], value['previous_id'])
    return 200, {'indexed': len(value['rows'])}
