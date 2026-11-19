"""Server-side network policy. No model-supplied networking authority."""
import ipaddress
import json
from pathlib import Path
import re

DEFAULTS = json.loads(Path(__file__).with_name('network_allowlist.json').read_text())
READ_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'CONNECT'})


def domain(value):
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError('invalid domain')
    value = value.rstrip('.').lower().encode('idna').decode('ascii')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
        raise ValueError('invalid domain')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in value.split('.')):
        raise ValueError('invalid domain')
    return value


def public_address(value):
    ip = ipaddress.ip_address(value)
    # Reject mapped/transition encodings too: a public-looking IPv6 address
    # must not smuggle a private IPv4 destination through the proxy.
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not (ip.version == 6 and (
                ip not in ipaddress.ip_network('2000::/3')
                or ip.ipv4_mapped or ip.sixtofour or ip.teredo)))


def network_decide(mode, host, method, project_extra=(), session=False):
    host = domain(host)
    if mode not in {'suggest', 'approve', 'auto', 'yolo'}:
        return 'deny'
    if mode == 'yolo' or session:
        return 'allow'
    if host in {*DEFAULTS['allowlist'], *project_extra} and (
            method in READ_METHODS or host in DEFAULTS['upload_hosts']):
        return 'allow'
    return 'ask'


def network_key(host, method):
    # Session approval binds the visible method. CONNECT cannot reveal methods
    # inside TLS; this unavoidable limitation is documented in the runtime guide.
    from .policy import allowance_key
    return allowance_key('network', {'domain': domain(host), 'method': method})
