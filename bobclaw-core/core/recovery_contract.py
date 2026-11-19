"""Closed, credential-free errors and native recovery verifier format (v1)."""
import base64
import hashlib
import json
import re

from .settings_contract import SettingsError, identifier, password_rules

PAIRS = {'password_stick', 'password_totp', 'totp_stick', 'setup'}


def digest(binding, recovery_id, secret):
    # Canonical JSON is ASCII with no whitespace. Secret is exactly 256 bits.
    if not isinstance(secret, str) or not re.fullmatch('[A-Z2-7]{52}', secret):
        raise SettingsError('invalid_factors')
    raw = base64.b32decode(secret + '====')
    if len(raw) != 32 or base64.b32encode(raw).decode().rstrip('=') != secret:
        raise SettingsError('invalid_factors')
    payload = ['bob-recovery-v1', binding['installation_id'], binding['lane_id'],
               binding['owner'], recovery_id, secret]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def validate(value):
    op = value.get('op')
    common = {'version', 'op', 'request_key', 'expected_epoch', 'context'}
    if op == 'factors.prepare':
        required = common | {'pair', 'replacement'}
        optional = {'old_stick', 'setup_capability', 'new_seed', 'new_password'}
        if not required <= set(value) or set(value) - required - optional:
            raise SettingsError('invalid_args')
        pair = value['pair']
        if not isinstance(pair, str) or pair not in PAIRS:
            raise SettingsError('invalid_args')
        replacement = value['replacement']
        if (not isinstance(replacement, dict) or set(replacement) != {'recovery_id', 'verifier'}
                or not isinstance(replacement['verifier'], str)
                or not re.fullmatch('[0-9a-f]{64}', replacement['verifier'])):
            raise SettingsError('invalid_args')
        identifier(replacement['recovery_id'])
        expected = {'old_stick'} if pair in {'password_stick', 'totp_stick'} else set()
        expected |= {'new_seed'} if pair in {'password_stick', 'setup'} else set()
        expected |= {'new_password'} if pair == 'totp_stick' else set()
        expected |= {'setup_capability'} if pair == 'setup' else set()
        if set(value) & optional != expected:
            raise SettingsError('invalid_args')
        if 'new_seed' in expected and (not isinstance(value['new_seed'], str)
                or not re.fullmatch('[A-Z2-7]{32}', value['new_seed'])):
            raise SettingsError('invalid_args')
        if 'new_password' in expected:
            password_rules(value['new_password'])
        if 'setup_capability' in expected:
            identifier(value['setup_capability'])
        if 'old_stick' in expected:
            stick = value['old_stick']
            if (not isinstance(stick, dict) or set(stick) != {'installation_id', 'lane_id', 'owner', 'recovery_id', 'secret'}):
                raise SettingsError('invalid_args')
            for k in stick:
                identifier(stick[k])
    elif op in {'factors.commit', 'factors.cancel'}:
        required = common | {'challenge_id'}
        if op == 'factors.commit':
            required |= {'material_saved', 'new_totp'}
        if set(value) != required:
            raise SettingsError('invalid_args')
        identifier(value['challenge_id'])
        if op == 'factors.commit' and (value['material_saved'] is not True
                or not isinstance(value['new_totp'], str) or not re.fullmatch('[0-9]{0,6}', value['new_totp'])):
            raise SettingsError('invalid_args')
    else:
        raise SettingsError('unsupported_operation')
    identifier(value['request_key'])
    if (type(value['expected_epoch']) is not int or value['expected_epoch'] < 1
            or not isinstance(value['context'], str) or not re.fullmatch('[0-9a-f]{64}', value['context'])):
        raise SettingsError('invalid_args')
    return value


def prepared_result(value):
    try:
        identifier(value['challenge_id'])
        if type(value['expires_at']) is not int or value['state'] != 'proof_verified':
            raise ValueError()
        return {k: value[k] for k in ('challenge_id', 'expires_at', 'state')}
    except (KeyError, ValueError, TypeError):
        raise SettingsError('invalid_receipt') from None


def binding_result(value):
    try:
        if set(value) != {'installation_id', 'lane_id', 'owner'}:
            raise ValueError()
        for item in value.values():
            identifier(item)
        return dict(value)
    except (ValueError, TypeError, AttributeError):
        raise SettingsError('invalid_receipt') from None
