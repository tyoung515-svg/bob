"""Additive chat catalog metadata and exact pin admission; no provider I/O."""

PROVIDER_LABELS = {
    'openai_codex': 'GPT (ChatGPT sign-in)',
    'claude_code': 'Claude subscription', 'codex_code': 'Codex coding agent',
    'claude_api': 'Claude API', 'deepseek_v4_flash': 'DeepSeek',
    'deepseek_v4': 'DeepSeek Pro', 'local': 'Local',
}
# Shared with the LangChain wrapper, without importing provider SDKs in setup.
TOOL_CAPABLE_BACKENDS = frozenset({'deepseek_v4_flash', 'glm_5_3', 'glm_5_3_flash_or',
                                  'qwen_research', 'qwen_cloud', 'openai_codex'})
SUBSCRIPTIONS = frozenset({'openai_codex', 'claude_code', 'codex_code', 'agy_code', 'kimi_code'})


def provider_label(row):
    backend, model = row.get('backend'), row.get('model')
    label = row.get('provider_label') or PROVIDER_LABELS.get(backend) or row.get('label') or backend
    suffix = ' / ' + model if isinstance(model, str) and model else ''
    return label[:-len(suffix)] if suffix and label.endswith(suffix) else label


def with_choices(row, *, observations=None):
    """Keep legacy fields byte-for-byte; never invent an unknown CLI model."""
    result = dict(row)
    selected = row.get('model')
    models = row.get('models', [selected] if selected else [])
    models = list(dict.fromkeys(m for m in models if isinstance(m, str) and m))
    default = selected if selected in models else next(iter(models), None)
    existing = row.get('choices') if observations is None else None
    observations = observations or {}
    result.update(provider_label=provider_label(row), choices=existing if isinstance(existing, list) else [{
        'model': m, 'label': m + (' · ' + observations[m] if observations.get(m) else ''),
        'default': m == default} for m in models],
        tool_capable=bool(row.get('tool_capable', row.get('backend') in TOOL_CAPABLE_BACKENDS)),
        billing=row.get('billing', 'subscription' if row.get('backend') in SUBSCRIPTIONS else
                        None if row.get('backend') in {'local', 'opencode_serve'} else 'api_key'))
    return result


def validate_pin(rows, backend, model):
    """A supplied model must be in the advertised choices, before any dispatch."""
    row = next((r for r in rows if isinstance(r, dict) and r.get('backend') == backend), None)
    if row is None:
        raise ValueError(f'{backend} is not configured in this lane.')
    choices = row.get('choices') if isinstance(row.get('choices'), list) else with_choices(row)['choices']
    if model and model not in {c['model'] for c in choices}:
        raise ValueError(f'The model "{model}" is not offered by {provider_label(row)}.')
    if not row.get('available'):
        raise ValueError(f"{provider_label(row)} is unavailable: " + str(
            row.get('reason') or row.get('unavailable_reason') or 'provider unavailable'))
    return row
