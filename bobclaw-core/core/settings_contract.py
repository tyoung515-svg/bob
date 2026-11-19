"""Closed settings wire schema shared by the root helper and gateway.

No application config, environment loading, or secret-bearing exception text.
"""
import hashlib
import re
import unicodedata

# Reviewed key providers enroll only their own preset. Codex uses its separate
# sign-in/API-key login flow. IDs never double as environment names.
PROVIDERS = {
    "anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_API_KEY",
    "glm": "ZAI_API_KEY", "kimi": "KIMI_API_KEY", "qwen": "DASHSCOPE_API_KEY",
}
PROVIDER_LABELS = {
    "anthropic": "Anthropic", "deepseek": "DeepSeek", "google": "Google",
    "glm": "GLM (Z.ai)", "kimi": "Kimi (Moonshot)",
    "qwen": "Qwen (Alibaba Model Studio)",
}
MAX_REQUEST = 32768


class SettingsError(ValueError):
    def __init__(self, code="settings_unavailable"):
        self.code = code
        super().__init__(code)


def decode(payload):
    import json
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SettingsError('invalid_args')
            value[key] = item
        return value
    return json.loads(payload, object_pairs_hook=unique)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SettingsError("invalid_args")
    return value


def validate(value, *, receipt=False):
    if not isinstance(value, dict) or value.get("version") != 1 or type(value.get("version")) is not int:
        raise SettingsError("invalid_args")
    op = value.get("op")
    if op in {'embedder.status', 'embedder.install'} and not receipt:
        expected = {'version', 'op'} | ({'model'} if op == 'embedder.install' else set())
        if set(value) != expected or (op == 'embedder.install' and
                (not isinstance(value['model'], str) or value['model'] not in {'4b', '0.6b'})):
            raise SettingsError('invalid_args')
    elif op == "status":
        if set(value) not in ({"version", "op", "request_key"}, {"version", "op", "operation_id"}):
            raise SettingsError("invalid_args")
        identifier(value.get("request_key", value.get("operation_id")))
    elif op == 'factors.binding' and not receipt:
        if set(value) != {'version', 'op'}:
            raise SettingsError('invalid_args')
    elif isinstance(op, str) and op.startswith('factors.') and not receipt:
        from .recovery_contract import validate as validate_factors
        return validate_factors(value)
    elif op == 'password.change' and not receipt:
        if set(value) != {'version', 'op', 'new', 'expected_epoch', 'request_key'}:
            raise SettingsError('invalid_args')
        identifier(value['request_key'])
        password_rules(value['new'])
        if type(value['expected_epoch']) is not int or value['expected_epoch'] < 1:
            raise SettingsError('invalid_args')
    elif op == "secrets.set" and not receipt:
        if set(value) != {"version", "op", "provider", "key", "request_key"}:
            raise SettingsError("invalid_args")
        identifier(value["request_key"])
        if not isinstance(value['provider'], str) or value["provider"] not in PROVIDERS:
            raise SettingsError("unsupported_provider")
        key = value["key"]
        if (not isinstance(key, str) or not key
                or any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in key)
                or len(key.encode("utf-8")) > 16384):
            raise SettingsError("invalid_key")
    else:
        # Generic stage/commit/restart and credential reads are never RPCs.
        raise SettingsError("unsupported_operation")
    return value


def password_rules(value, current=None):
    if (not isinstance(value, str) or not 10 <= len(value) <= 1024
            or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in value)
            or len(value.encode('utf-8')) > 4096
            or value == current):
        raise SettingsError('invalid_password')
    return value


def password_result(value):
    try:
        if (type(value['changed']) is not bool or type(value['restarted']) is not bool
                or (value['restarted'] and not value['changed'])):
            raise ValueError()
        return {k: value[k] for k in ('changed', 'restarted')}
    except (KeyError, TypeError, ValueError):
        raise SettingsError('invalid_receipt') from None


def fingerprint(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def secret_result(value):
    """Never pass a helper/upstream dictionary through to an API caller."""
    try:
        if (value["provider"] not in PROVIDERS or value["stored"] is not True
                or not re.fullmatch(r"[0-9a-f]{16}", value["fingerprint"])):
            raise ValueError()
        identifier(value["operation_id"])
        return {k: value[k] for k in ("provider", "stored", "fingerprint", "operation_id")}
    except (KeyError, TypeError, ValueError):
        raise SettingsError("invalid_receipt") from None


def status_result(value):
    try:
        identifier(value["operation_id"])
        if value["phase"] not in {"prepared", "committed", "restart-pending", "ready", "failed"}:
            raise ValueError()
        result = {k: value[k] for k in ("operation_id", "phase")}
        if value.get('operation') in {'password.change', 'factors.commit', 'factors.cancel'} and value['phase'] in {'ready', 'failed'}:
            result['result'] = password_result(value['result'])
        elif value.get('operation') == 'factors.prepare' and value['phase'] == 'ready':
            from .recovery_contract import prepared_result
            result['result'] = prepared_result(value['result'])
        elif value["phase"] == "ready":
            result["result"] = secret_result(value["result"])
            if result['result']['operation_id'] != result['operation_id']:
                raise ValueError()
        elif value["phase"] == "failed":
            result["error"] = "settings_failed"
        return result
    except (KeyError, TypeError, ValueError):
        raise SettingsError("invalid_receipt") from None
