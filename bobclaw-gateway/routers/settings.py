"""Owner-only write-only settings. All failures use fixed credential-free codes."""
import asyncio
from aiohttp import web

from core.execution.models import ExecutionError, only
from core.settings_contract import (SettingsError, PROVIDERS, PROVIDER_LABELS, secret_result, decode,
                                    password_rules, password_result, identifier)
from core import settings_client
from owner_sessions import authenticate

router = web.RouteTableDef()


def capabilities():
    from core.execution.owner import unavailable_reason as owner_reason
    reason = owner_reason() or settings_client.unavailable_reason()
    result = {name: {'version': 2, 'contract_versions': [2], 'implemented': True,
                   'available': reason is None, 'reason': reason,
                   'owner_session_required': True, 'request_key_required': name != 'policy.get',
                   **({'providers': sorted(PROVIDERS), 'storage_only': True,
                       # Compatibility marker for the existing write-only key UI.
                       'enrolls_backend': True,
                       'provider_display': [{'id': p, 'label': PROVIDER_LABELS[p]}
                                            for p in sorted(PROVIDERS)],
                       'credential_retry': 'never', 'receipt_socket_version': 1}
                      if name == 'secrets.set' else {})}
            for name in ('secrets.set', 'policy.get', 'policy.set')}
    from core import credential_state
    password_reason = reason
    from config import config
    if not config.TOTP_SECRET or not config.BOBCLAW_PASSWORD:
        password_reason = 'enrolled_password_totp_required'
    if password_reason is None:
        try:
            credential_state.require_loaded()
            if not credential_state.enabled():
                password_reason = 'credential_generation_required'
        except SettingsError:
            password_reason = 'credential_generation_unavailable'
    result['auth.change_password'] = dict(version=2, contract_versions=[2], implemented=True,
        available=password_reason is None, reason=password_reason, owner_context_required=True,
        owner_session_required=False, fresh_factors_required=True, request_key_required=True,
        credential_retry='never', receipt_socket_version=1)
    from routers.recovery import available
    for name in ('auth.factors.binding', 'auth.factors.prepare', 'auth.factors.commit', 'auth.factors.cancel'):
        result[name] = dict(version=2, contract_versions=[2], implemented=True, available=available(),
            reason=None if available() else 'credential_generation_or_helper_required',
            owner_session_required=False, owner_context_required=True, credential_retry='never',
            request_key_required=not name.endswith('binding'), receipt_socket_version=1,
            native_sensitive_required=True)
    return result


@router.post('/settings/password')
async def change_password(request):
    from core import credential_state
    from credential_auth import proof, require_loaded
    from owner_sessions import portal
    try:
        require_loaded()
        if not capabilities()['auth.change_password']['available']:
            raise SettingsError('settings_unavailable')
        # Portal ownership is context, never factor proof. This single form is
        # bound to its request key and spends a fresh TOTP only once.
        if request.headers.get('Authorization', '').startswith('Bearer bos1.'):
            claims = await authenticate(request, 'owner.status')
            dimension = [claims['portal_credential_id'], claims['broker_link_nonce']]
        else:
            claims, link = portal(request)
            dimension = [claims['jti'], link]
        body = await request.json(loads=decode)
        only(body, {'current', 'new', 'totp', 'request_key', 'contract_version'})
        if type(body.get('contract_version')) is not int or body['contract_version'] != 2:
            raise SettingsError('invalid_args')
        key = identifier(body.get('request_key'))
        password_rules(body.get('new'), body.get('current'))
        if not isinstance(body.get('current'), str) or len(body['current']) > 1024:
            raise SettingsError('invalid_args')
        if not isinstance(body.get('totp'), str) or len(body['totp']) > 16:
            raise SettingsError('invalid_args')
        epoch = credential_state.loaded_epoch()
        if not await proof(body['current'], body['totp'], 'password.change', dimension, request_key=key):
            return web.json_response({'changed': False, 'restarted': False})
        result = await asyncio.to_thread(settings_client.call, dict(version=1, op='password.change',
            new=body['new'], expected_epoch=epoch, request_key=key))
        return web.json_response(password_result(result))
    except ExecutionError as exc:
        response = web.json_response({'error': {'code': exc.code}}, status=exc.status)
        if exc.status == 429:
            response.headers['Retry-After'] = str(exc.detail['retry_after'])
        return response
    except SettingsError as exc:
        code = exc.code if exc.code in {'request_key_used', 'invalid_args', 'invalid_password', 'invalid_receipt'} else 'settings_unavailable'
        return web.json_response({'error': {'code': code}}, status=409 if code == 'request_key_used' else 422 if code in {'invalid_args', 'invalid_password'} else 503)
    except Exception:
        return web.json_response({'error': {'code': 'settings_unavailable'}}, status=503)


@router.get('/auth/generation')
async def generation_ready(request):
    from core import credential_state
    try:
        if request.remote not in {'127.0.0.1', '::1'} or not credential_state.enabled():
            return web.json_response({'ready': False}, status=503)
        credential_state.require_loaded()
        return web.json_response({'ready': True, 'credential_epoch': credential_state.loaded_epoch()})
    except SettingsError:
        return web.json_response({'ready': False}, status=503)


@router.post('/settings/secrets')
async def set_secret(request):
    try:
        await authenticate(request, 'secrets.set')
        if settings_client.unavailable_reason():
            raise SettingsError('settings_unavailable')
        body = await request.json(loads=decode)
        only(body, {'provider', 'key', 'request_key', 'contract_version'})
        if type(body.get('contract_version')) is not int or body['contract_version'] != 2:
            raise SettingsError('invalid_args')
        value = {k: v for k, v in body.items() if k != 'contract_version'}
        result = await asyncio.to_thread(settings_client.call, {'version': 1, 'op': 'secrets.set', **value})
        return web.json_response(secret_result(result))
    except ExecutionError as exc:
        return web.json_response({'error': {'code': exc.code}}, status=exc.status)
    except SettingsError as exc:
        status = 409 if exc.code == 'request_key_used' else 422 if exc.code in {'invalid_args', 'invalid_key', 'unsupported_provider'} else 503
        return web.json_response({'error': {'code': exc.code}}, status=status)
    except Exception:
        # Do not log repr/traceback: parsers, helpers and HTTP clients may echo keys.
        return web.json_response({'error': {'code': 'settings_unavailable'}}, status=503)


@router.get('/settings/policy')
async def get_policy(request):
    from routers.execution import owner_read
    if settings_client.unavailable_reason():
        return web.json_response({'error': {'code': 'settings_unavailable'}}, status=503)
    return await owner_read(request, 'policy.get')


@router.post('/settings/policy')
async def set_policy(request):
    from routers.execution import owner_command
    if settings_client.unavailable_reason():
        return web.json_response({'error': {'code': 'settings_unavailable'}}, status=503)
    return await owner_command(request, 'policy.set', {'policy', 'expected_revision', 'request_key'})
