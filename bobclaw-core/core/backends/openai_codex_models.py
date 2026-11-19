"""Sanitized account model names; one ranking rule for setup and inference.

No credentials, requests, or lane writes here. The account default means the
first visible entry by explicit default, then priority, then slug.
"""
import math
import re


def model_slug(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', value))


def visible_model_slugs(catalog):
    from core.backends.openai_codex_auth import CodexProviderError
    entries = catalog.get('models') if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        raise CodexProviderError('invalid model catalog')
    candidates = [m for m in entries if isinstance(m, dict)
                  and model_slug(m.get('slug')) and m.get('visibility') not in {'hide', 'hidden'}]
    def rank(m):
        priority = m.get('priority')
        return (0 if m.get('is_default') is True else 1,
                priority if type(priority) in (int, float) and math.isfinite(priority) else 10000,
                m['slug'])
    models = list(dict.fromkeys(m['slug'] for m in sorted(candidates, key=rank)))
    if not models:
        raise CodexProviderError('account catalog has no visible model; choose a model after signing in')
    return models


def enrolled_models(models, selected=None):
    """Bound the published snapshot; retain the selected pin even beyond slot 12."""
    result = list(dict.fromkeys(m for m in models if model_slug(m)))[:12]
    if selected and selected not in result:
        result = result[:11] + [selected]
    return tuple(result)
