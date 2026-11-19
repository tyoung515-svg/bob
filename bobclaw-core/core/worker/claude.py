"""Pinned Claude subscription enrollment contract; never an API fallback."""
import json
import re
from urllib.parse import urlsplit

EXECUTABLE = '/usr/local/bin/claude'
CREDENTIAL = '.credentials.json'
# Account default is an explicit choice, not a claim about its resolved model.
# 2.1.327 --model help explicitly names these three aliases as examples.
MODELS = ('default', 'fable', 'opus', 'sonnet')
LINK_HOSTS = frozenset({'claude.com', 'claude.ai'})
MAX_CODE = 4096
MAX_URL = 8192


def environment(config, home):
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home),
            'CLAUDE_CONFIG_DIR': str(config), 'TMPDIR': str(home),
            'DISABLE_AUTOUPDATER': '1',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
            'ENABLE_CLAUDEAI_MCP_SERVERS': 'false'}


def signed_in(value):
    # Subscription only. Console OAuth, injected tokens and API keys do not enroll.
    return (isinstance(value, dict) and value.get('loggedIn') is True
            and value.get('authMethod') == 'claude.ai'
            and value.get('apiProvider') == 'firstParty')


def status_json(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > 65536:
        raise ValueError('invalid Claude status')
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get('loggedIn')) is not bool:
        raise ValueError('invalid Claude status')
    return {'signed_in': signed_in(value), 'logged_out': value['loggedIn'] is False}


def signin_url(line):
    # Parse complete lines only; never emit a partial query from a split read.
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
    for match in re.finditer(r'https://[^\s<>"\x00-\x20]+', clean):
        url = match.group()
        if len(url) > MAX_URL or not url.isascii() or '\\' in url:
            continue
        try:
            parts = urlsplit(url)
            if (parts.scheme == 'https' and parts.hostname in LINK_HOSTS
                    and parts.netloc == parts.hostname and not parts.fragment
                    and not parts.username and not parts.password):
                return url
        except ValueError:
            pass
    return None


def code_line(raw):
    # OAuth's manual code can include a #state suffix. No whitespace or controls.
    if not isinstance(raw, bytes) or not raw.endswith(b'\n'):
        raise ValueError('invalid sign-in code')
    value = raw[:-1]
    if value.endswith(b'\r'):
        value = value[:-1]
    if not 1 <= len(value) <= MAX_CODE or not re.fullmatch(rb'[A-Za-z0-9._~+/#=%:-]+', value):
        raise ValueError('invalid sign-in code')
    return value + b'\n'


def code_rejected(output):
    return any(word in output.lower() for word in (
        'invalid authorization code', 'invalid_grant', 'code expired',
        'expired code', 'invalid code', 'state mismatch', 'invalid state'))
