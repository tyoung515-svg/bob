"""No network: exact approved pins and configured membership stay separate."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'configured_model_relay',
    Path(__file__).resolve().parents[1] / 'scripts/server/workbench-model-relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def test_allowed_set_and_pins_are_unchanged():
    assert relay.ALLOWED == {
        'bob-ds-flash': ('DSv4.1 Flash', 'deepseek-v4-flash'),
        'bob-gpt-sol-61': ('GPT SOL 6.1', 'gpt-6.1-sol'),
    }


@pytest.mark.parametrize('data', [[], [
    {'id': 'bob-qwen-strata', 'bob': {'enabled': True}},
    {'id': 'bob-gpt-sol-61', 'bob': {
        'enabled': True, 'model': 'different-pin', 'transport': 'text'}},
]])
def test_unconfigured_alias_is_reported_without_substitution(data):
    calls = []
    def exchange(method, path, payload):
        calls.append((method, path, payload))
        return {'data': data}
    result = relay.handle({'op': 'catalog'}, exchange)
    assert calls == [('GET', '/v1/models', None)]
    assert {row['id'] for row in result['models']} == set(relay.ALLOWED)
    for row in result['models']:
        assert row['configured'] is False
        assert 'Not set up' in row['reason'] and 'no fallback' in row['reason']
        assert row['model_pin'] == relay.ALLOWED[row['id']][1]


def test_configured_alias_requires_enabled_exact_pin_and_text_transport():
    data = [
        {'id': 'bob-ds-flash', 'bob': {
            'enabled': True, 'model': 'deepseek-v4-flash', 'transport': 'text'}},
        {'id': 'bob-gpt-sol-61', 'bob': {
            'enabled': True, 'model': 'gpt-6.1-sol', 'transport': 'vision'}},
        {'id': 'not-authorized', 'bob': {'enabled': True}},
    ]
    result = relay.handle({'op': 'catalog'}, lambda *args: {'data': data})
    assert [row['id'] for row in result['models'] if row['configured']] == ['bob-ds-flash']
    assert all(row['reachability'] == 'unverified' for row in result['models'])


def test_completions_refuse_other_aliases_and_returned_pin_mismatches():
    messages = [{'role': 'user', 'content': 'offline fixture'}]
    for alias in ['bob-qwen-strata', 'deepseek-v4-pro', 'unknown']:
        with pytest.raises(ValueError, match='unapproved'):
            relay.handle({'op': 'complete', 'model': alias, 'messages': messages},
                         lambda *args: pytest.fail('unapproved alias reached transport'))
    calls = []
    def wrong_model(method, path, payload):
        calls.append(payload['model'])
        return {'model': 'bob-ds-flash', 'bob': {'model_pin': 'deepseek-v4-flash'}}
    with pytest.raises(ValueError, match='mismatch_no_fallback'):
        relay.handle({'op': 'complete', 'model': 'bob-gpt-sol-61', 'messages': messages}, wrong_model)
    assert calls == ['bob-gpt-sol-61']
