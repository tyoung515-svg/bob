"""Content-free provider failure vocabulary shared by chat and workflow stages.

Code mappings: OpenAI Codex codex-api/src/sse/responses_error.rs and error.rs;
incomplete reasons: OpenAI Responses streaming reference. Unknown codes stay unknown.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import re
import time

logger = logging.getLogger(__name__)
SENTENCES = {
    'model_rate_limited': 'The model is rate limited for the moment.',
    'model_usage_limit': "The plan's usage limit has been reached.",
    'model_context_limit': 'The conversation is too long for this model.',
    'model_output_limit': 'The reply was cut off at the output limit.',
    'model_provider_error': 'The model provider had an error.',
    'model_content_rules': "The reply was stopped by the provider's content rules.",
    'model_auth_rejected': 'The model provider denied access to this request.',
    'model_stopped': 'The model stopped the reply for an unrecognized reason.',
}
CODE_CATEGORIES = {
    'rate_limit_exceeded': 'model_rate_limited', 'slow_down': 'model_rate_limited',
    'usage_limit_reached': 'model_usage_limit', 'insufficient_quota': 'model_usage_limit',
    'credit_balance_exhausted': 'model_usage_limit',
    'organization_spend_limit_exceeded': 'model_usage_limit',
    'project_spend_limit_exceeded': 'model_usage_limit', 'usage_not_included': 'model_usage_limit',
    'context_length_exceeded': 'model_context_limit',
    'server_error': 'model_provider_error', 'server_is_overloaded': 'model_provider_error',
    'policy_capacity_unavailable': 'model_provider_error',
    'content_policy_violation': 'model_content_rules', 'cyber_policy': 'model_content_rules',
    'bio_policy': 'model_content_rules', 'misalignment_policy_violation': 'model_content_rules',
}


def token(value):
    # Codes are identifiers, never prose. repr/logs must not carry provider messages.
    if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value):
        return value
    return 'redacted_identifier' if value is not None else None


def seconds(value, *, now=None):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            result = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, result) if math.isfinite(result) else None


def reset_time(value):
    try:
        if isinstance(value, bool) or value is None:
            return None
        stamp = float(value)
        return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (ValueError, TypeError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class ProviderFailure:
    code: str = 'model_stopped'
    event_type: str | None = None
    provider_code: str | None = None
    provider_type: str | None = None
    incomplete_reason: str | None = None
    retry_after: float | None = None
    resets_at: str | None = None
    status: int | None = None

    @property
    def sentence(self):
        if self.code == 'model_usage_limit' and self.resets_at:
            return "The plan's usage limit has been reached; it resets at " + self.resets_at + '.'
        return SENTENCES[self.code]

    @property
    def retryable(self):
        return self.code in {'model_rate_limited', 'model_provider_error'}

    def log(self):
        logger.warning('Model failure category=%s event_type=%s provider_code=%s provider_type=%s '
                       'incomplete_reason=%s retry_after_seconds=%s resets_at=%s status=%s',
                       self.code, self.event_type, self.provider_code, self.provider_type,
                       self.incomplete_reason, self.retry_after, self.resets_at, self.status)


def from_payload(payload=None, *, event_type=None, status=None, headers=None, fallback=None):
    payload = payload if isinstance(payload, dict) else {}
    response = payload.get('response')
    response = response if isinstance(response, dict) else payload
    error = response.get('error')
    error = error if isinstance(error, dict) else response
    details = response.get('incomplete_details') or {}
    reason = token(details.get('reason')) if isinstance(details, dict) else None
    code, kind = token(error.get('code')), token(error.get('type'))
    # The top-level event type is not an error type.
    if error is payload and kind == event_type:
        kind = None
    category = CODE_CATEGORIES.get(code or kind, 'model_stopped')
    if code and code not in CODE_CATEGORIES:
        category = 'model_stopped'
    elif reason == 'content_filter':
        category = 'model_content_rules'
    elif reason == 'max_output_tokens':
        category = 'model_output_limit'
    elif not code and not kind:
        category = ('model_rate_limited' if status == 429 else
                    'model_auth_rejected' if status in {401, 403} else
                    'model_provider_error' if status is not None and status >= 500 else
                    fallback or 'model_stopped')
    # Read only named timing fields; never keep/log arbitrary headers or messages.
    delay_values = []
    for source in (headers, error.get('headers'), response.get('headers'), payload.get('headers')):
        if hasattr(source, 'items'):
            timing = {str(k).lower(): v for k, v in source.items()
                      if str(k).lower() in {'retry-after', 'retry-after-ms'}}
            delay_values.append(seconds(timing.get('retry-after')))
            ms = seconds(timing.get('retry-after-ms'))
            delay_values.append(ms / 1000 if ms is not None else None)
    for source in (error, response, payload):
        delay_values.append(seconds(source.get('retry_after')))
        ms = seconds(source.get('retry_after_ms'))
        delay_values.append(ms / 1000 if ms is not None else None)
    # Codex CLI parses this narrow phrase only for rate-limit errors.
    if category == 'model_rate_limited' and isinstance(error.get('message'), str):
        match = re.search(r'(?i)try again in\s*(\d+(?:\.\d+)?)\s*(ms|seconds?|s)\b', error['message'])
        if match:
            parsed = seconds(match[1])
            if parsed is not None:
                delay_values.append(parsed / (1000 if match[2].lower() == 'ms' else 1))
    delay = max((v for v in delay_values if v is not None), default=None)
    return ProviderFailure(category, token(event_type), code, kind, reason, delay,
                           reset_time(error.get('resets_at')), status)


def describe_failure(exc):
    failure = getattr(exc, 'failure', None)
    if isinstance(failure, ProviderFailure):
        return failure
    # Raw API-key transports raise before yielding HTTP content; SDK tool calls
    # expose status/body. No string matching of arbitrary exception messages.
    import aiohttp
    if isinstance(exc, aiohttp.ClientResponseError):
        return from_payload(status=exc.status, event_type='http.error', headers=exc.headers)
    try:
        from openai import APIStatusError
        if isinstance(exc, APIStatusError):
            return from_payload(exc.body, status=exc.status_code, event_type='http.error',
                                headers=exc.response.headers)
    except ImportError:
        pass
    return None
