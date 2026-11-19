"""Offline configured-lane catalogs through the real broker normalization."""
import pytest

from test_catalog_degrade import broker_module, make, PATHS


@pytest.mark.parametrize('rows', [
    [],
    [
        {'backend': 'openai_codex', 'label': 'GPT (ChatGPT sign-in)',
         'model': None, 'available': True},
        {'backend': 'codex_code', 'label': 'Codex configured default',
         'model': None, 'available': False, 'unavailable_reason': 'Codex CLI missing'},
    ],
    [
        {'backend': 'claude_api', 'label': 'Anthropic', 'model': 'configured-claude',
         'available': True},
        {'backend': 'deepseek_v4_flash', 'label': 'DeepSeek',
         'model': 'configured-deepseek', 'available': False, 'reason': 'Key expired'},
    ],
    [{'backend': 'deployment_backend', 'label': 'Deployment label',
      'model': 'deployment-pin', 'available': True}],
])
def test_membership_labels_null_models_and_reasons(broker_module, rows):
    reply = make(broker_module, {
        '/faces': [], '/models/available': rows, '/backends/catalog': rows,
        '/capabilities': {},
    }).op_catalog({})
    assert reply['faces'] == []
    assert [row['id'] for row in reply['models']] == [row['backend'] for row in rows]
    assert [row['id'] for row in reply['backends']] == [row['backend'] for row in rows]
    assert reply['capabilities']['backends'] == len(rows)
    assert reply['capabilities']['faces'] == 0
    assert reply['capabilities']['warnings'] == []
    for original, model, backend in zip(rows, reply['models'], reply['backends']):
        for result in (model, backend):
            assert result['label'] == original['label']
            assert result['model'] == original['model']
            assert result['available'] == original['available']
        reason = original.get('unavailable_reason') or original.get('reason')
        assert model['reason'] == reason
        assert backend['unavailable_reason'] == reason


def test_partial_catalog_retains_label_null_and_authorization(broker_module):
    outcomes = {path: 'gateway_http_500' for path in PATHS}
    outcomes['/models/available'] = [
        {'backend': 'codex_code', 'label': 'Codex configured default',
         'model': None, 'available': False, 'unavailable_reason': 'Sign in again'}]
    outcomes['/faces'] = [{'id': 'assistant'}, {'id': 'reviewer'}]
    broker = make(broker_module, outcomes)
    broker.identity['faces'] = ['assistant']
    reply = broker.op_catalog({})
    assert reply['models'][0]['label'] == 'Codex configured default'
    assert reply['models'][0]['model'] is None
    assert reply['models'][0]['reason'] == 'Sign in again'
    assert reply['backends'] == []
    assert [row['allowed'] for row in reply['faces']] == [True, False]
    assert 'backends: gateway_http_500' in reply['capabilities']['warnings']
